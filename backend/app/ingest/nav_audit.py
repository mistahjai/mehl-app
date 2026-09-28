"""Coverage audit of stored MF/ETF NAVs against AMFI's portal history.

The AMFI portal is the only AMFI host reachable from here (www.amfiindia.com drops
TCP on 443), and it serves history, not just the daily snapshot. That makes it the
independent authority for checking whether what we store is actually complete --
separate from the source that wrote it.

`audit_range` is read-only: it fetches, diffs, and reports. `backfill_missing` is
the one write path, and it is deliberately narrow -- it inserts only (symbol, ts)
keys that do not exist yet. It never overwrites a value and never deletes a row, so
restatements and stale rows have to be dealt with deliberately rather than as a
side effect of closing a coverage gap.
"""

import logging
from datetime import date

import polars as pl

from app.engine import db as market_db
from app.ingest.amfi import (
    PORTAL_CHUNK_DAYS,
    PORTAL_TP,
    collect_portal_history,
    portal_chunks,
)

logger = logging.getLogger(__name__)

# AMFI publishes NAVs to 4 decimal places. Comparing floats with a bare
# `abs(a - b) > 1e-4` is wrong at the boundary: a one-unit change in the last
# published digit *is* 1e-4, and the subtraction lands just above or just below
# it depending on the values, so a rounding wobble reads as corruption on some
# rows and not others. Counting whole published units instead is exact.
NAV_DECIMALS = 4
# A single unit in the last published place is noise; anything larger is a
# restatement or a bad ingest and worth surfacing.
NAV_TOLERANCE_UNITS = 1


def _diff_units(amfi: pl.Series, stored: pl.Series) -> pl.Series:
    """Absolute difference measured in units of the last published decimal."""
    return ((amfi - stored).abs() * (10**NAV_DECIMALS)).round()


def _stored_navs(start: date, end: date, symbols: list[str]) -> pl.DataFrame:
    """Our stored (symbol, ts, close) for the given symbols inside the range."""
    empty = pl.DataFrame(
        schema={"symbol": pl.Utf8, "ts": pl.Date, "close": pl.Float64}
    )
    if not symbols:
        return empty
    with market_db.get_connection() as con:
        frame = con.execute(
            """
            SELECT symbol, ts, close
            FROM ohlcv
            WHERE series = 'MF' AND ts BETWEEN ? AND ?
            """,
            [start, end],
        ).pl()
    if frame.is_empty():
        return empty
    # DuckDB hands back ts as datetime[us] while the parsed portal frame uses
    # Date; without this cast the join below refuses on mismatched key types.
    return frame.filter(pl.col("symbol").is_in(symbols)).select(
        pl.col("symbol").cast(pl.Utf8), pl.col("ts").cast(pl.Date), "close"
    )


def _diff(
    amfi: pl.DataFrame, stored: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Split the two frames into (missing, extra, mismatched), keyed by (symbol, ts).

    `amfi` is treated as the authority. Returns keys for missing/extra and full rows
    for mismatched so a caller can sample the differing values.
    """
    amfi_keys = amfi.select(
        pl.col("symbol").cast(pl.Utf8), pl.col("ts").cast(pl.Date), "close"
    )
    # Rename before joining: a full join with coalesce=True keeps the left name
    # and suffixes the right one, which is easy to get wrong. Naming them up
    # front keeps the column references below unambiguous.
    stored_keys = stored.select(
        pl.col("symbol").cast(pl.Utf8),
        pl.col("ts").cast(pl.Date),
        pl.col("close").alias("stored_close"),
    )

    joined = amfi_keys.join(stored_keys, on=["symbol", "ts"], how="full", coalesce=True)
    missing = joined.filter(pl.col("stored_close").is_null()).select("symbol", "ts")
    extra = joined.filter(pl.col("close").is_null()).select("symbol", "ts")
    both = joined.filter(
        pl.col("close").is_not_null() & pl.col("stored_close").is_not_null()
    ).filter(_diff_units(pl.col("close"), pl.col("stored_close")) > NAV_TOLERANCE_UNITS)
    return missing, extra, both


def _per_symbol_counts(frame: pl.DataFrame) -> dict[str, int]:
    if frame.is_empty():
        return {}
    return {
        row[0]: row[1]
        for row in frame.group_by("symbol").len().select("symbol", "len").rows()
    }


def audit_range(
    start: date,
    end: date,
    chunk_days: int = PORTAL_CHUNK_DAYS,
    tps: tuple[int, ...] = PORTAL_TP,
) -> dict:
    """Diff AMFI portal NAVs against stored rows over [start, end].

    Reports three distinct problems, which need different fixes:
      missing   -- AMFI has a NAV we do not        (backfill)
      extra     -- we have a NAV AMFI does not     (stale or retired scheme)
      mismatched-- both have the day, values differ (bad ingest or a restatement)
    """
    windows = list(portal_chunks(start, end, chunk_days))
    amfi, errors = collect_portal_history(start, end, tps, chunk_days=chunk_days)
    if amfi.is_empty():
        return {
            "start": start,
            "end": end,
            "chunks": len(windows),
            "tps": tps,
            "amfi_rows": 0,
            "stored_rows": 0,
            "missing": 0,
            "extra": 0,
            "mismatched": 0,
            "missing_by_symbol": {},
            "extra_by_symbol": {},
            "mismatch_sample": [],
            "errors": errors,
        }

    symbols = amfi["symbol"].unique().to_list()
    stored = _stored_navs(start, end, symbols)
    if stored.is_empty():
        return {
            "start": start,
            "end": end,
            "chunks": len(windows),
            "tps": tps,
            "amfi_rows": amfi.height,
            "stored_rows": 0,
            "missing": amfi.height,
            "extra": 0,
            "mismatched": 0,
            "missing_by_symbol": _per_symbol_counts(amfi),
            "extra_by_symbol": {},
            "mismatch_sample": [],
            "errors": errors,
        }

    missing, extra, mismatched = _diff(amfi, stored)

    return {
        "start": start,
        "end": end,
        "chunks": len(windows),
        "tps": tps,
        "amfi_rows": amfi.height,
        "stored_rows": stored.height,
        "missing": missing.height,
        "extra": extra.height,
        "mismatched": mismatched.height,
        "missing_by_symbol": _per_symbol_counts(missing),
        "extra_by_symbol": _per_symbol_counts(extra),
        "mismatch_sample": [
            {
                "symbol": r["symbol"],
                "ts": r["ts"],
                "amfi": r["close"],
                "stored": r["stored_close"],
            }
            for r in mismatched.head(50).to_dicts()
        ],
        "errors": errors,
    }


def backfill_missing(
    start: date,
    end: date,
    chunk_days: int = PORTAL_CHUNK_DAYS,
    tps: tuple[int, ...] = PORTAL_TP,
    dry_run: bool = False,
) -> dict:
    """Insert the (symbol, ts) NAVs AMFI has and we do not, for [start, end].

    Additive by construction. Two guards make that true:

    * Only keys absent from `ohlcv` are selected, and they go in through
      `insert_ohlcv` (ON CONFLICT DO NOTHING), so an existing row is never
      rewritten. Restatements -- where both sides hold a day but disagree -- are
      reported by the audit and left alone deliberately.
    * The normal ingest path is bypassed on purpose. `_drop_regressions` keeps
      only rows past each symbol's high-water mark, which is right for daily runs
      but would silently discard exactly these rows: a hole in the middle of a
      history is older than the newest row we already hold. Pushing the full
      frame through `ingest_portal_history` would have reported success while
      writing almost nothing.

    Nothing is deleted, so `extra` rows are also left untouched.
    """
    windows = list(portal_chunks(start, end, chunk_days))
    amfi, errors = collect_portal_history(start, end, tps, chunk_days=chunk_days)
    if amfi.is_empty():
        logger.warning("nav-backfill: portal returned nothing for %s..%s", start, end)
        return {
            "start": start,
            "end": end,
            "chunks": len(windows),
            "tps": tps,
            "amfi_rows": 0,
            "missing": 0,
            "inserted": 0,
            "dry_run": dry_run,
            "errors": errors,
        }

    symbols = amfi["symbol"].unique().to_list()
    stored = _stored_navs(start, end, symbols)
    missing, _extra, _mismatched = _diff(amfi, stored)

    if missing.is_empty():
        return {
            "start": start,
            "end": end,
            "chunks": len(windows),
            "tps": tps,
            "amfi_rows": amfi.height,
            "missing": 0,
            "inserted": 0,
            "dry_run": dry_run,
            "errors": errors,
        }

    # Rejoin the missing keys onto the full portal frame to recover the complete
    # OHLCV row; _diff only carries the keys forward.
    rows = (
        amfi.join(missing, on=["symbol", "ts"], how="inner")
        .select("symbol", "ts", "open", "high", "low", "close", "volume", "series")
    )
    inserted = 0 if dry_run else market_db.insert_ohlcv(rows)
    logger.info(
        "nav-backfill %s..%s: %d missing, %d inserted (dry_run=%s)",
        start,
        end,
        missing.height,
        inserted,
        dry_run,
    )
    return {
        "start": start,
        "end": end,
        "chunks": len(windows),
        "tps": tps,
        "amfi_rows": amfi.height,
        "missing": missing.height,
        "inserted": inserted,
        "dry_run": dry_run,
        "errors": errors,
    }
