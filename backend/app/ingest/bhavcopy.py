import io
import logging
import zipfile
from datetime import date, timedelta

import polars as pl

from app.engine import db as market_db
from app.ingest.http import NO_RETRY_404_STATUSES, RateLimitedSession
from app.ingest.universe import MAINBOARD_SERIES, sync_securities

logger = logging.getLogger(__name__)

UDIFF_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
UDIFF_URL_ALT = "https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
LEGACY_URL = "https://archives.nseindia.com/content/cm/cm{ddmmmyyyy}bhav.csv.zip"
LEGACY_URL_ALT = "https://nsearchives.nseindia.com/content/cm/cm{ddmmmyyyy}bhav.csv.zip"

UDIFF_CUTOVER = date(2024, 7, 8)

_INSTRUMENT_TYPE_MAP = {"STK": "stock", "ETF": "etf", "IND": "index", "MFI": "mf", "UND": None}


def _normalize(df: pl.DataFrame) -> pl.DataFrame:
    if "instrument_type" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.String).alias("instrument_type"))
    return df.select("symbol", "ts", "open", "high", "low", "close", "volume", "series", "instrument_type")


def _read_zip_csv(content: bytes) -> pl.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        name = zf.namelist()[0]
        return pl.read_csv(
            zf.read(name), encoding="utf8-lossy", infer_schema_length=10_000, truncate_ragged_lines=True
        )


def parse_udiff(df: pl.DataFrame) -> pl.DataFrame:
    out = df.filter(
        pl.col("SctySrs").is_in(sorted(MAINBOARD_SERIES)) & pl.col("TckrSymb").is_not_null()
    ).select(
        pl.col("TckrSymb").str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.col("TradDt").str.to_date("%Y-%m-%d", strict=False).alias("ts"),
        pl.col("OpnPric").cast(pl.Float64, strict=False).alias("open"),
        pl.col("HghPric").cast(pl.Float64, strict=False).alias("high"),
        pl.col("LwPric").cast(pl.Float64, strict=False).alias("low"),
        pl.col("ClsPric").cast(pl.Float64, strict=False).alias("close"),
        pl.col("TtlTradgVol").cast(pl.Int64, strict=False).alias("volume"),
        pl.col("SctySrs").str.strip_chars().str.to_uppercase().alias("series"),
        pl.col("FinInstrmTp")
        .str.strip_chars()
        .str.to_uppercase()
        .replace_strict(_INSTRUMENT_TYPE_MAP, default=None)
        .alias("instrument_type"),
    )
    return _normalize(out.filter(pl.col("ts").is_not_null()))


def parse_legacy(content: bytes, day: date) -> pl.DataFrame:
    df = _read_zip_csv(content)
    df.columns = [c.strip() for c in df.columns]
    out = df.filter(pl.col("SERIES").is_in(sorted(MAINBOARD_SERIES))).select(
        pl.col("SYMBOL").str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.lit(day).alias("ts"),
        pl.col("OPEN").cast(pl.Float64, strict=False).alias("open"),
        pl.col("HIGH").cast(pl.Float64, strict=False).alias("high"),
        pl.col("LOW").cast(pl.Float64, strict=False).alias("low"),
        pl.col("CLOSE").cast(pl.Float64, strict=False).alias("close"),
        pl.col("TOTTRDQTY").cast(pl.Int64, strict=False).alias("volume"),
        pl.col("SERIES").str.strip_chars().str.to_uppercase().alias("series"),
        pl.lit(None, dtype=pl.String).alias("instrument_type"),
        pl.col("ISIN").str.strip_chars().alias("isin"),
    )
    return _normalize(out.drop("isin"))


def fetch_bhavcopy(session: RateLimitedSession, day: date) -> pl.DataFrame | None:
    yyyymmdd = f"{day:%Y%m%d}"
    ddmmmyyyy = day.strftime("%d%b%Y").upper()
    urls = {
        "udiff": [
            UDIFF_URL.format(yyyymmdd=yyyymmdd),
            UDIFF_URL_ALT.format(yyyymmdd=yyyymmdd),
        ],
        "legacy": [
            LEGACY_URL.format(ddmmmyyyy=ddmmmyyyy),
            LEGACY_URL_ALT.format(ddmmmyyyy=ddmmmyyyy),
        ],
    }

    attempts = [("udiff", urls["udiff"]), ("legacy", urls["legacy"])]
    if day < UDIFF_CUTOVER:
        attempts.reverse()

    for kind, candidates in attempts:
        for url in candidates:
            res = session.get(url)
            if res.status_code != 200 or not res.content:
                continue
            try:
                raw = _read_zip_csv(res.content)
                if kind == "udiff":
                    df = parse_udiff(raw)
                else:
                    df = parse_legacy(res.content, day)
            except Exception as e:
                logger.warning("failed to parse %s (%s): %s", url, kind, e)
                continue
            if not df.is_empty():
                return df
    return None


def ingest_bhavcopy(start: date, end: date, rate: float = 0.5) -> int:
    # A 404 here means the file for that date does not exist, so it is not
    # retried -- see NO_RETRY_404_STATUSES.
    session = RateLimitedSession(min_interval=rate, retry_statuses=NO_RETRY_404_STATUSES)
    total_rows = 0
    days = 0
    seen: list[pl.DataFrame] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            df = fetch_bhavcopy(session, day)
            if df is not None:
                total_rows += market_db.merge_ohlcv(df)
                seen.append(df.with_columns(pl.lit(day).alias("last_seen")))
                days += 1
                logger.info("bhavcopy %s: %d rows (total %d rows, %d days)", day, df.height, total_rows, days)
            else:
                logger.info("bhavcopy %s: not found (holiday or unavailable)", day)
        day += timedelta(days=1)
    # Sync once for the whole window rather than per day: each day repeats most
    # of the previous day's symbols, so the per-day upserts were redundant work
    # on top of already being row-by-row. Days are appended in ascending order, so
    # keep="last" leaves the newest row per symbol. Every column has to survive --
    # sync_securities upserts isin/series/instrument_type, so reducing to a key
    # would null them out.
    if seen:
        combined = pl.concat(seen, how="diagonal_relaxed").unique(
            subset=["symbol"], keep="last", maintain_order=True
        )
        sync_securities(combined)
    logger.info("bhavcopy done: %d days, %d rows", days, total_rows)
    return total_rows
