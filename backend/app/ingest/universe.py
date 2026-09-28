import logging
import re
from datetime import date

import polars as pl

from app.ingest.http import RateLimitedSession

logger = logging.getLogger(__name__)

EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

MAINBOARD_SERIES = frozenset({"EQ", "BE", "BZ"})
SME_SERIES = frozenset({"SM", "ST", "SZ"})

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def parse_equity_list(content: bytes) -> pl.DataFrame:
    df = pl.read_csv(content, encoding="utf8-lossy", infer_schema_length=10_000)
    df.columns = [c.strip() for c in df.columns]

    symbol_col = next(c for c in df.columns if c.upper().startswith("SYMBOL"))
    name_col = next(c for c in df.columns if "NAME" in c.upper())
    series_col = next(c for c in df.columns if "SERIES" in c.upper())
    isin_col = next(c for c in df.columns if "ISIN" in c.upper())
    listing_col = next((c for c in df.columns if "LISTING" in c.upper()), None)

    out = df.select(
        pl.col(symbol_col).str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.col(name_col).str.strip_chars().alias("name"),
        pl.col(series_col).str.strip_chars().str.to_uppercase().alias("series"),
        pl.col(isin_col).str.strip_chars().alias("isin"),
        pl.col(listing_col).alias("listed_on_raw") if listing_col else pl.lit(None).alias("listed_on_raw"),
    ).filter(
        pl.col("series").is_in(sorted(MAINBOARD_SERIES)) & pl.col("symbol").is_not_null()
    )
    out = out.with_columns(
        pl.when(pl.col("name").str.to_uppercase().str.contains("ETF"))
        .then(pl.lit("etf"))
        .otherwise(pl.lit("stock"))
        .alias("instrument_type")
    )
    if listing_col:
        out = out.with_columns(
            pl.col("listed_on_raw")
            .str.strip_chars()
            .str.to_date("%d-%b-%Y", strict=False)
            .alias("listed_on")
        ).drop("listed_on_raw")
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Date).alias("listed_on"))
    return out.select("symbol", "name", "series", "isin", "instrument_type", "listed_on")


def sync_securities(df: pl.DataFrame) -> int:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import Security

    if df.is_empty():
        return 0

    rows: list[dict] = []
    for row in df.iter_rows(named=True):
        isin = row.get("isin")
        amfi_code = row.get("amfi_code")
        rows.append(
            {
                "symbol": row["symbol"],
                "isin": isin if isinstance(isin, str) and _ISIN_RE.match(isin) else None,
                "name": row.get("name"),
                "series": row.get("series"),
                "instrument_type": row.get("instrument_type") or "stock",
                "amfi_code": str(amfi_code) if amfi_code else None,
                "listed_on": row.get("listed_on"),
                "last_seen": row.get("last_seen") or date.today(),
            }
        )

    # One statement for the whole batch. This used to execute per row, which
    # meant a 7-day bhavcopy window issued ~14k separate upserts and dominated
    # the daily run. Deduplicating on symbol first matters because SQLite's
    # ON CONFLICT would otherwise take the *first* row for a repeated symbol,
    # silently discarding the other days' data in the same batch.
    unique: dict[str, dict] = {}
    for row in rows:
        unique[row["symbol"]] = row

    with SessionLocal() as session:
        stmt = sqlite_insert(Security).values(list(unique.values()))
        stmt = stmt.on_conflict_do_update(
            index_elements=[Security.symbol],
            set_={
                "isin": stmt.excluded.isin,
                "series": stmt.excluded.series,
                "instrument_type": stmt.excluded.instrument_type,
                "amfi_code": stmt.excluded.amfi_code,
                "listed_on": stmt.excluded.listed_on,
                "last_seen": stmt.excluded.last_seen,
                "is_active": True,
            },
        )
        session.execute(stmt)
        session.commit()
    return len(unique)


def build_universe(rate: float = 0.5) -> int:
    session = RateLimitedSession(min_interval=rate)
    res = session.get(EQUITY_LIST_URL)
    res.raise_for_status()
    df = parse_equity_list(res.content)
    n = sync_securities(df)
    logger.info("universe: %d mainboard instruments synced", n)
    return n


def all_symbols() -> list[str]:
    from app.state.db import SessionLocal
    from app.state.models import Security

    with SessionLocal() as session:
        rows = session.query(Security.symbol).order_by(Security.symbol).all()
    return [r[0] for r in rows]
