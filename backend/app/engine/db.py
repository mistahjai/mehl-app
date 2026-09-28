from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
import time

import duckdb
import polars as pl

from app.config import settings

_OHLCV_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol VARCHAR NOT NULL,
    ts TIMESTAMP NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    series VARCHAR,
    PRIMARY KEY (symbol, ts)
)
"""

_INDICES_SCHEMA = """
CREATE TABLE IF NOT EXISTS indices (
    symbol VARCHAR NOT NULL,
    ts TIMESTAMP NOT NULL,
    close DOUBLE,
    PRIMARY KEY (symbol, ts)
)
"""


@contextmanager
def get_connection() -> Iterator[duckdb.DuckDBPyConnection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(settings.market_db_path))
    try:
        _ensure_schema(con)
        yield con
    finally:
        con.close()


@contextmanager
def _owned_or_given(
    con: duckdb.DuckDBPyConnection | None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield `con` if the caller supplied one, else open a short-lived connection.

    Lets a write helper be called either standalone or many times against a
    single already-open connection (batching many small writes into one
    connect/disconnect cycle instead of one per row batch).
    """
    if con is not None:
        yield con
    else:
        with get_connection() as owned:
            yield owned


_schema_checked = False


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    global _schema_checked
    con.execute(_OHLCV_SCHEMA)
    con.execute(_INDICES_SCHEMA)
    if not _schema_checked:
        cols = {
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'ohlcv'"
            ).fetchall()
        }
        if "series" not in cols:
            con.execute("ALTER TABLE ohlcv ADD COLUMN series VARCHAR")
        _schema_checked = True


def _validate_ohlcv(df: pl.DataFrame) -> pl.DataFrame:
    required = {"symbol", "ts", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"missing OHLCV columns: {sorted(missing)}")
    if "series" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.String).alias("series"))
    return df


def replace_ohlcv(df: pl.DataFrame, *, con: duckdb.DuckDBPyConnection | None = None) -> int:
    """DESTRUCTIVE: delete every stored row for each symbol in the batch, then insert.

    Only correct for a *full-range reload* of those symbols (i.e. the batch
    contains their entire history). Passing an incremental batch -- a single
    trading day, or the last N days -- silently discards all older rows for every
    symbol it touches. Incremental ingest must use merge_ohlcv instead.

    Also O(rows x symbols) to run: DuckDB has no secondary index, so the
    per-symbol DELETEs each scan the whole table.
    """
    df = _validate_ohlcv(df)
    if df.is_empty():
        return 0
    with _owned_or_given(con) as active:
        symbols = df["symbol"].unique().to_list()
        active.executemany("DELETE FROM ohlcv WHERE symbol = ?", [(s,) for s in symbols])
        active.execute(
            "INSERT INTO ohlcv (symbol, ts, open, high, low, close, volume, series) "
            "SELECT symbol, ts, open, high, low, close, volume, series FROM df"
        )
    return df.height


def merge_ohlcv(df: pl.DataFrame, *, con: duckdb.DuckDBPyConnection | None = None) -> int:
    """Insert or update per (symbol, ts): the incoming values win on conflict,
    and rows for days not in the batch are preserved (unlike replace_ohlcv)."""
    df = _validate_ohlcv(df)
    if df.is_empty():
        return 0
    with _owned_or_given(con) as active:
        active.execute(
            "INSERT INTO ohlcv (symbol, ts, open, high, low, close, volume, series) "
            "SELECT symbol, ts, open, high, low, close, volume, series FROM df "
            "ON CONFLICT (symbol, ts) DO UPDATE SET "
            "open = excluded.open, high = excluded.high, low = excluded.low, "
            "close = excluded.close, volume = excluded.volume, series = excluded.series"
        )
    return df.height


def insert_ohlcv(df: pl.DataFrame, *, con: duckdb.DuckDBPyConnection | None = None) -> int:
    df = _validate_ohlcv(df)
    if df.is_empty():
        return 0
    with _owned_or_given(con) as active:
        result = active.execute(
            "INSERT INTO ohlcv (symbol, ts, open, high, low, close, volume, series) "
            "SELECT symbol, ts, open, high, low, close, volume, series FROM df "
            "ON CONFLICT (symbol, ts) DO NOTHING"
        ).fetchall()
    return int(result[0][0]) if result else 0


def fetch_ohlcv(
    symbol: str, start: str | None = None, end: str | None = None, limit: int = 5000
) -> pl.DataFrame:
    with get_connection() as con:
        query = "SELECT * FROM ohlcv WHERE symbol = ?"
        params: list[object] = [symbol]
        if start is not None:
            query += " AND ts >= ?"
            params.append(start)
        if end is not None:
            query += " AND ts <= ?"
            params.append(end)
        query += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        return con.execute(query, params).pl()


def fetch_all(limit: int = 1_000_000) -> pl.DataFrame:
    with get_connection() as con:
        return con.execute("SELECT * FROM ohlcv ORDER BY symbol, ts ASC LIMIT ?", [limit]).pl()


def upsert_indices(df: pl.DataFrame) -> int:
    if df.is_empty():
        return 0
    with get_connection() as con:
        symbols = df["symbol"].unique().to_list()
        con.executemany("DELETE FROM indices WHERE symbol = ?", [(s,) for s in symbols])
        con.execute(
            "INSERT INTO indices (symbol, ts, close) "
            "SELECT symbol, ts, close FROM df"
        )
    return df.height


def insert_indices(df: pl.DataFrame) -> int:
    if df.is_empty():
        return 0
    with get_connection() as con:
        result = con.execute(
            "INSERT INTO indices (symbol, ts, close) "
            "SELECT symbol, ts, close FROM df "
            "ON CONFLICT (symbol, ts) DO NOTHING"
        ).fetchall()
    return int(result[0][0]) if result else 0


def fetch_indices(
    symbol: str, start: str | None = None, end: str | None = None, limit: int = 100_000
) -> pl.DataFrame:
    with get_connection() as con:
        query = "SELECT symbol, ts, close FROM indices WHERE symbol = ?"
        params: list[object] = [symbol]
        if start is not None:
            query += " AND ts >= ?"
            params.append(start)
        if end is not None:
            query += " AND ts <= ?"
            params.append(end)
        query += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        return con.execute(query, params).pl()


def list_indices() -> list[str]:
    with get_connection() as con:
        rows = con.execute("SELECT DISTINCT symbol FROM indices ORDER BY symbol").fetchall()
    return [r[0] for r in rows]


# A series needs this many bars in the last year before a chart or benchmark
# comparison against it is meaningful. Lifetime row count is not a usable signal:
# retired NSE names and a handful of ^CNX* symbols that Yahoo serves a single bar
# for both look well populated while having a multi-year hole at the end.
MIN_USABLE_INDEX_ROWS = 30
USABLE_INDEX_WINDOW_DAYS = 365


def index_summary(max_age_days: int = 7) -> pl.DataFrame:
    """One row per index series: row count, date range, latest close, and whether
    it is still being maintained (latest bar within max_age_days).

    The table also holds retired NSE series that only exist as history, so the UI
    needs the freshness flag to decide what to offer as selectable. `is_usable`
    additionally requires enough recent bars to plot or benchmark against.
    """
    with get_connection() as con:
        return con.execute(
            "WITH latest AS (SELECT max(ts) AS mx FROM indices), "
            "recent AS (SELECT symbol, count(*) AS recent_rows FROM indices, latest "
            "  WHERE ts >= latest.mx - INTERVAL 365 DAY GROUP BY symbol), "
            "agg AS (SELECT symbol, count(*) AS rows, min(ts) AS first_ts, max(ts) AS last_ts, "
            "  arg_max(close, ts) AS last_close FROM indices GROUP BY symbol) "
            "SELECT a.symbol, a.rows, a.first_ts, a.last_ts, a.last_close, "
            f"a.last_ts >= (SELECT mx FROM latest) - INTERVAL {int(max_age_days)} DAY AS is_live, "
            f"a.last_ts >= (SELECT mx FROM latest) - INTERVAL {int(max_age_days)} DAY "
            f"AND coalesce(r.recent_rows, 0) >= {MIN_USABLE_INDEX_ROWS} AS is_usable "
            "FROM agg a LEFT JOIN recent r USING (symbol) ORDER BY a.symbol"
        ).pl()


def fetch_latest_snapshot() -> pl.DataFrame:
    """Latest close/volume per symbol in one vectorized scan (scales to all rows)."""
    with get_connection() as con:
        return con.execute(
            "SELECT symbol, arg_max(close, ts) AS close, arg_max(volume, ts) AS volume, "
            "arg_max(series, ts) AS series, max(ts) AS last_ts "
            "FROM ohlcv GROUP BY symbol ORDER BY symbol"
        ).pl()


def fetch_coverage() -> pl.DataFrame:
    """First/last price date per symbol across ohlcv and indices (search helper)."""
    with get_connection() as con:
        return con.execute(
            "SELECT symbol, min(ts) AS date_first_entry, max(ts) AS date_last_entry "
            "FROM (SELECT symbol, ts FROM ohlcv UNION ALL SELECT symbol, ts FROM indices) "
            "GROUP BY symbol ORDER BY symbol"
        ).pl()


_COVERAGE_TTL_SECONDS = 300.0
_coverage_cache: tuple[float, pl.DataFrame] | None = None


def cached_coverage() -> pl.DataFrame:
    """fetch_coverage() with a short in-process TTL (scales to search traffic)."""
    global _coverage_cache
    now = time.monotonic()
    if _coverage_cache is not None and now - _coverage_cache[0] < _COVERAGE_TTL_SECONDS:
        return _coverage_cache[1]
    frame = fetch_coverage()
    _coverage_cache = (now, frame)
    return frame


def reset_coverage_cache() -> None:
    """Drop the cached coverage frame so the next search recomputes it."""
    global _coverage_cache
    _coverage_cache = None


def fetch_close_on(symbol: str, day: date) -> float | None:
    """Close on the exact date, else the nearest previous trading day."""
    with get_connection() as con:
        row = con.execute(
            "SELECT close FROM ohlcv WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            [symbol, day],
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None
