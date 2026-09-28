import logging
import re
import zipfile
from datetime import date
from pathlib import Path

import polars as pl

from app.engine import db as market_db

logger = logging.getLogger(__name__)

MAINBOARD_SERIES = frozenset({"EQ", "BE", "BZ"})

_EMPTY_SCRIP = pl.DataFrame(
    schema={
        "symbol": pl.String,
        "ts": pl.Datetime,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
        "series": pl.String,
    }
)

_EMPTY_INDEX = pl.DataFrame(schema={"symbol": pl.String, "ts": pl.Datetime, "close": pl.Float64})


def slugify_index(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.strip().upper()).strip("_")


def parse_scrip_csv(content: bytes) -> pl.DataFrame:
    """Parse one legacy bhavcopy-style SCRIP csv (Date,Symbol,Series,...)."""
    try:
        df = pl.read_csv(content, infer_schema_length=10_000)
    except Exception as e:
        logger.warning("zip: scrip csv parse failed: %s", e)
        return _EMPTY_SCRIP
    df.columns = [c.strip() for c in df.columns]
    required = {"Date", "Symbol", "Series", "Close"}
    if not required.issubset(set(df.columns)):
        return _EMPTY_SCRIP
    exprs = [
        pl.col("Date").str.strip_chars().str.to_date("%Y-%m-%d", strict=False).cast(pl.Datetime).alias("ts"),
        pl.col("Symbol").str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.col("Series").str.strip_chars().str.to_uppercase().alias("series"),
        pl.col("Close").cast(pl.Float64, strict=False).alias("close"),
        (
            pl.col("Open").cast(pl.Float64, strict=False)
            if "Open" in df.columns
            else pl.lit(None, dtype=pl.Float64)
        ).alias("open"),
        (
            pl.col("High").cast(pl.Float64, strict=False)
            if "High" in df.columns
            else pl.lit(None, dtype=pl.Float64)
        ).alias("high"),
        (
            pl.col("Low").cast(pl.Float64, strict=False)
            if "Low" in df.columns
            else pl.lit(None, dtype=pl.Float64)
        ).alias("low"),
        (
            pl.col("Volume").cast(pl.Int64, strict=False)
            if "Volume" in df.columns
            else pl.lit(None, dtype=pl.Int64)
        ).alias("volume"),
    ]
    out = df.select(exprs).filter(
        pl.col("ts").is_not_null()
        & pl.col("close").is_not_null()
        & pl.col("series").is_in(sorted(MAINBOARD_SERIES))
    )
    return out.select(_EMPTY_SCRIP.columns) if not out.is_empty() else _EMPTY_SCRIP


def parse_index_csv(content: bytes, name: str) -> pl.DataFrame:
    """Parse one INDEX csv (Date,Open,High,Low,Close,Volume,Turnover)."""
    try:
        df = pl.read_csv(content, infer_schema_length=10_000)
    except Exception as e:
        logger.warning("zip: index csv %s parse failed: %s", name, e)
        return _EMPTY_INDEX
    df.columns = [c.strip() for c in df.columns]
    if "Date" not in df.columns or "Close" not in df.columns:
        return _EMPTY_INDEX
    out = df.select(
        pl.col("Date").str.strip_chars().str.to_date("%Y-%m-%d", strict=False).cast(pl.Datetime).alias("ts"),
        pl.col("Close").cast(pl.Float64, strict=False).alias("close"),
    ).filter(pl.col("ts").is_not_null() & pl.col("close").is_not_null())
    if out.is_empty():
        return _EMPTY_INDEX
    return out.with_columns(pl.lit(name).alias("symbol")).select(_EMPTY_INDEX.columns)


def sync_archive_securities(symbol_info: dict[str, tuple[str, date]]) -> int:
    """Register zip symbols in the securities master without wiping
    existing names/ISINs (the zip has no company names)."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import Security

    if not symbol_info:
        return 0
    with SessionLocal() as session:
        for symbol, (series, last) in sorted(symbol_info.items()):
            stmt = sqlite_insert(Security).values(
                symbol=symbol,
                series=series,
                last_seen=last,
                instrument_type="stock",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[Security.symbol],
                set_={
                    "series": stmt.excluded.series,
                    "last_seen": stmt.excluded.last_seen,
                },
            )
            session.execute(stmt)
        session.commit()
    return len(symbol_info)


def import_zip(path: Path, batch_rows: int = 1_500_000) -> dict:
    """Import SCRIP + INDEX csvs from an NSE dataset zip into DuckDB.

    Additive: existing (symbol, ts) rows are never overwritten.
    """
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    scrip_names = sorted(n for n in names if "/SCRIP/" in n and n.endswith(".csv"))
    index_names = sorted(n for n in names if "/INDEX/" in n and n.endswith(".csv"))
    if not scrip_names and not index_names:
        raise ValueError(f"no SCRIP/ or INDEX/ csvs found in {path}")

    scrip_rows = 0
    index_rows = 0
    pending: list[pl.DataFrame] = []
    pending_count = 0
    symbol_info: dict[str, tuple[str, date]] = {}

    def flush() -> None:
        nonlocal pending, pending_count, scrip_rows
        if not pending:
            return
        combined = pl.concat(pending)
        scrip_rows += market_db.insert_ohlcv(combined)
        info = combined.group_by("symbol").agg(
            pl.col("series").last(), pl.col("ts").max().alias("last")
        )
        for r in info.iter_rows(named=True):
            prev = symbol_info.get(r["symbol"])
            last = r["last"].date()
            if prev is None or last > prev[1]:
                symbol_info[r["symbol"]] = (r["series"], last)
        pending, pending_count = [], 0

    for i, name in enumerate(scrip_names, 1):
        df = parse_scrip_csv(zf.read(name))
        if df.is_empty():
            continue
        pending.append(df)
        pending_count += df.height
        if pending_count >= batch_rows:
            flush()
            logger.info("zip: scrip %d/%d files, %d rows inserted so far", i, len(scrip_names), scrip_rows)
        elif i % 300 == 0:
            logger.info("zip: scrip %d/%d files parsed", i, len(scrip_names))
    flush()

    index_frames: list[pl.DataFrame] = []
    for name in index_names:
        index_name = slugify_index(name.split("/")[-1].removesuffix(".csv"))
        df = parse_index_csv(zf.read(name), index_name)
        if not df.is_empty():
            index_frames.append(df)
    if index_frames:
        index_rows = market_db.insert_indices(pl.concat(index_frames))

    securities_synced = sync_archive_securities(symbol_info)

    logger.info(
        "zip: done - %d ohlcv rows, %d index rows, %d securities synced",
        scrip_rows,
        index_rows,
        securities_synced,
    )
    return {
        "ohlcv_rows": scrip_rows,
        "index_rows": index_rows,
        "securities_synced": securities_synced,
        "scrip_files": len(scrip_names),
        "index_files": len(index_names),
    }
