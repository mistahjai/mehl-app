import logging
from datetime import date, timedelta

import polars as pl
import yfinance as yf

from app.engine import db as market_db

logger = logging.getLogger(__name__)

# Canonical name -> candidate yfinance tickers, tried in order until one returns
# data. Names match the ones already in the DuckDB `indices` table so an
# existing series is extended rather than duplicated.
#
# NSE retired/renamed several of these and Yahoo dropped the Nifty Midcap
# family entirely, so the list only carries symbols verified to still resolve.
# Legacy names present in the table (NIFTY_100, NIFTY_200, NIFTY_MIDCAP_50,
# NIFTY_GS_*, NIFTY100_*, NIFTY50_*_LEV/INV, ...) are kept as history only and
# are intentionally absent here -- they get no daily update.
INDICES: dict[str, list[str]] = {
    # --- India: headline ---
    "NIFTY_50": ["^NSEI"],
    "NIFTY_500": ["^CRSLDX"],
    "SENSEX": ["^BSESN"],
    "INDIA_VIX": ["^INDIAVIX"],
    # --- India: breadth / size ---
    "NIFTY_NEXT_50": ["^NSEMDCP50"],
    # --- India: sectors ---
    "NIFTY_BANK": ["^NSEBANK"],
    "NIFTY_FIN_SERVICE": ["^CNXFIN", "^CNXFINANCE"],
    "NIFTY_IT": ["^CNXIT"],
    "NIFTY_FMCG": ["^CNXFMCG"],
    "NIFTY_PHARMA": ["^CNXPHARMA"],
    "NIFTY_AUTO": ["^CNXAUTO"],
    "NIFTY_METAL": ["^CNXMETAL"],
    "NIFTY_REALTY": ["^CNXREALTY"],
    "NIFTY_PSE": ["^CNXPSE"],
    "NIFTY_PSU_BANK": ["^CNXPSUBANK"],
    "NIFTY_MNC": ["^CNXMNC"],
    "NIFTY_MEDIA": ["^CNXMEDIA"],
    "NIFTY_INFRA": ["^CNXINFRA"],
    "NIFTY_ENERGY": ["^CNXENERGY"],
    # --- global benchmarks ---
    "SPX_500": ["^GSPC"],
    "DOW_JONES": ["^DJI"],
    "NASDAQ": ["^IXIC"],
    "US_VIX": ["^VIX"],
    "NIKKEI_225": ["^N225"],
    "HANG_SENG": ["^HSI"],
    "DAX": ["^GDAXI"],
    "FTSE_100": ["^FTSE"],
}

# How stale a series may be before the UI stops offering it as selectable.
LIVE_INDEX_MAX_AGE_DAYS = 7

# A fetch returning fewer rows than this is treated as a Yahoo stub response and
# retried over a shorter recent window.
MIN_ROWS_PER_FETCH = 20
FALLBACK_WINDOW_DAYS = 365 * 5

_EMPTY = pl.DataFrame(
    schema={"symbol": pl.String, "ts": pl.Datetime, "close": pl.Float64}
)


def _canonical_name(yahoo_symbol: str) -> str | None:
    for name, candidates in INDICES.items():
        if yahoo_symbol in candidates:
            return name
    return None


def _history(yahoo_symbol: str, start: date, end: date):
    return yf.Ticker(yahoo_symbol).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        raise_errors=False,
    )


def fetch_index_close(yahoo_symbol: str, start: date, end: date) -> pl.DataFrame:
    """Fetch daily index closes from yfinance and tag them with the canonical name.

    Several ^CNX* sector symbols return a single stub row when asked for a
    multi-decade range, so a thin result is retried over a recent window --
    without that they end up with one row and a chart that looks broken.
    """
    name = _canonical_name(yahoo_symbol)
    if name is None:
        return _EMPTY
    hist = _history(yahoo_symbol, start, end)
    if hist is None or len(hist) < MIN_ROWS_PER_FETCH:
        recent = _history(yahoo_symbol, max(start, end - timedelta(days=FALLBACK_WINDOW_DAYS)), end)
        if recent is not None and len(recent) > 0 and (hist is None or len(recent) > len(hist)):
            hist = recent
    if hist is None or hist.empty:
        return _EMPTY
    df = pl.from_pandas(hist.reset_index())
    return df.select(
        pl.lit(name).alias("symbol"),
        pl.col("Date").dt.replace_time_zone(None).cast(pl.Datetime).alias("ts"),
        pl.col("Close").cast(pl.Float64, strict=False).alias("close"),
    )


def ingest_indices(start: date, end: date) -> int:
    total = 0
    failed: list[str] = []
    for name, candidates in INDICES.items():
        df = _EMPTY
        for yahoo_symbol in candidates:
            df = fetch_index_close(yahoo_symbol, start, end)
            if not df.is_empty():
                break
        if df.is_empty():
            logger.warning("indices: no data for %s (tried %s)", name, candidates)
            failed.append(name)
            continue
        n = market_db.insert_indices(df)
        total += n
        logger.info("indices: %s %d rows (%d new)", name, df.height, n)
    if failed:
        logger.warning("indices: %d/%d series unavailable: %s", len(failed), len(INDICES), failed)
    return total
