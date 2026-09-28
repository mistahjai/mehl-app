import logging
import re
from datetime import date
from pathlib import Path

import polars as pl

from app.engine import db as market_db
from app.ingest.zip_archive import sync_archive_securities

logger = logging.getLogger(__name__)

MAINBOARD_SERIES = frozenset({"EQ", "BE", "BZ"})

FNAME_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{4})\.csv$", re.IGNORECASE)
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

SCHEMA = {
    "symbol": pl.String,
    "ts": pl.Datetime,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "series": pl.String,
}


def fname_to_date(name: str) -> date | None:
    m = FNAME_RE.match(name)
    if not m:
        return None
    day, mon, year = int(m.group(1)), MONTHS[m.group(2).upper()], int(m.group(3))
    try:
        return date(year, mon, day)
    except ValueError:
        return None


def _pick(df: pl.DataFrame, candidates: list[str]) -> pl.Expr | None:
    for c in candidates:
        if c in df.columns:
            return pl.col(c)
    return None


def _num(col: pl.Expr, df: pl.DataFrame, target: type) -> pl.Expr:
    """Cast to a numeric type, stripping whitespace for string columns."""
    name = col.meta.output_name()
    if df.schema[name] == pl.String:
        return col.str.strip_chars().cast(target, strict=False)
    return col.cast(target, strict=False)


def parse_bhavcopy_csv(content: bytes, day: date) -> pl.DataFrame:
    """Parse one daily bhavcopy csv in any of the folder's formats:
    legacy (OPEN/CLOSE/TOTTRDQTY), nsepython-style (OPEN_PRICE/CLOSE_PRICE/
    TTL_TRD_QNTY), or UDiFF (TradDt/TckrSymb/ClsPric)."""
    try:
        df = pl.read_csv(content, infer_schema_length=5_000)
    except Exception:
        df = pl.read_csv(content, infer_schema_length=5_000, truncate_ragged_lines=True)
    df.columns = [c.strip() for c in df.columns]

    symbol = _pick(df, ["SYMBOL", "TckrSymb"])
    series = _pick(df, ["SERIES", "SctySrs"])
    close = _pick(df, ["CLOSE", "CLOSE_PRICE", "ClsPric"])
    if symbol is None or series is None or close is None:
        return pl.DataFrame(schema=SCHEMA)

    selects: list[pl.Expr] = [
        symbol.str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.lit(day).cast(pl.Datetime).alias("ts"),
    ]
    for canonical, candidates in {
        "open": ["OPEN", "OPEN_PRICE", "OpnPric"],
        "high": ["HIGH", "HIGH_PRICE", "HghPric"],
        "low": ["LOW", "LOW_PRICE", "LwPric"],
    }.items():
        col = _pick(df, candidates)
        if col is not None:
            selects.append(_num(col, df, float).alias(canonical))
        else:
            selects.append(pl.lit(None, dtype=pl.Float64).alias(canonical))
    selects.append(_num(close, df, float).alias("close"))
    vol = _pick(df, ["TOTTRDQTY", "TTL_TRD_QNTY", "TtlTradgVol"])
    if vol is not None:
        selects.append(_num(vol, df, int).alias("volume"))
    else:
        selects.append(pl.lit(None, dtype=pl.Int64).alias("volume"))
    selects.append(series.str.strip_chars().str.to_uppercase().alias("series"))

    out = df.select(selects).filter(
        pl.col("ts").is_not_null()
        & pl.col("close").is_not_null()
        & pl.col("series").is_in(sorted(MAINBOARD_SERIES))
    )
    if out.is_empty():
        return pl.DataFrame(schema=SCHEMA)
    return (
        out.group_by("symbol", "ts").agg(*[pl.col(c).first() for c in list(SCHEMA.keys())[2:]])
        .select(list(SCHEMA.keys()))
    )


def import_dir(path: Path, batch_rows: int = 1_500_000) -> dict:
    """Import daily bhavcopy csvs from a directory into DuckDB.

    Merge semantics: incoming (raw bhavcopy) values win on (symbol, ts)
    conflicts; rows for days not present in the folder are preserved.
    """
    files: list[tuple[Path, date]] = []
    for f in sorted(path.glob("*.csv")):
        d = fname_to_date(f.name)
        if d is None:
            logger.warning("bhavcopy-dir: unparseable filename: %s", f.name)
            continue
        files.append((f, d))
    if not files:
        raise ValueError(f"no DDMMMYYYY.csv files found in {path}")

    rows_inserted = 0
    pending: list[pl.DataFrame] = []
    pending_count = 0
    symbol_info: dict[str, tuple[str, date]] = {}

    def flush() -> None:
        nonlocal pending, pending_count, rows_inserted
        if not pending:
            return
        combined = pl.concat(pending)
        rows_inserted += market_db.merge_ohlcv(combined)
        info = combined.group_by("symbol").agg(
            pl.col("series").last(), pl.col("ts").max().alias("last")
        )
        for r in info.iter_rows(named=True):
            prev = symbol_info.get(r["symbol"])
            last = r["last"].date()
            if prev is None or last > prev[1]:
                symbol_info[r["symbol"]] = (r["series"], last)
        pending, pending_count = [], 0

    for i, (f, d) in enumerate(files, 1):
        try:
            df = parse_bhavcopy_csv(f.read_bytes(), d)
        except Exception as e:
            logger.error("bhavcopy-dir: parse failed %s: %s", f.name, e)
            continue
        if df.is_empty():
            continue
        pending.append(df)
        pending_count += df.height
        if pending_count >= batch_rows:
            flush()
            logger.info(
                "bhavcopy-dir: %d/%d files (%s), %d rows merged so far",
                i, len(files), d.isoformat(), rows_inserted,
            )
    flush()

    securities_synced = sync_archive_securities(symbol_info)
    logger.info(
        "bhavcopy-dir: done - %d rows merged across %d files, %d securities synced",
        rows_inserted,
        len(files),
        securities_synced,
    )
    return {
        "rows_merged": rows_inserted,
        "files": len(files),
        "securities_synced": securities_synced,
    }
