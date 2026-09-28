import logging
from datetime import UTC, date, datetime, timedelta

import polars as pl
import yfinance as yf

from app.engine import db as market_db
from app.ingest.http import RateLimitedSession

logger = logging.getLogger(__name__)

STATEMENTS = (
    ("income", "get_income_stmt"),
    ("balance", "get_balance_sheet"),
    ("cashflow", "get_cashflow"),
)


def fetch_history(symbol: str, start: date, end: date) -> pl.DataFrame:
    """Fetch raw (unadjusted) daily OHLCV from yfinance for one NSE symbol."""
    tk = yf.Ticker(f"{symbol}.NS")
    hist = tk.history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        raise_errors=False,
    )
    if hist is None or hist.empty:
        return pl.DataFrame()
    df = pl.from_pandas(hist.reset_index())
    return df.select(
        pl.lit(symbol).alias("symbol"),
        pl.col("Date").dt.replace_time_zone(None).cast(pl.Date).cast(pl.Datetime).alias("ts"),
        pl.col("Open").cast(pl.Float64, strict=False).alias("open"),
        pl.col("High").cast(pl.Float64, strict=False).alias("high"),
        pl.col("Low").cast(pl.Float64, strict=False).alias("low"),
        pl.col("Close").cast(pl.Float64, strict=False).alias("close"),
        pl.col("Volume").cast(pl.Int64, strict=False).alias("volume"),
    )


def fetch_financials(symbol: str) -> pl.DataFrame:
    """Fetch quarterly + annual income/balance/cashflow statements via yfinance."""
    tk = yf.Ticker(f"{symbol}.NS")
    rows: list[dict] = []
    for statement, method in STATEMENTS:
        getter = getattr(tk, method, None)
        if getter is None:
            continue
        for period_type, freq in (("annual", "yearly"), ("quarterly", "quarterly")):
            try:
                table = getter(freq=freq)
            except Exception as e:
                logger.warning("financials %s %s %s: %s", symbol, statement, period_type, e)
                continue
            if table is None or table.empty:
                continue
            for period in table.columns:
                period_end = period.to_pydatetime().date() if hasattr(period, "to_pydatetime") else period
                for line_item, value in table[period].items():
                    rows.append(
                        {
                            "symbol": symbol,
                            "period_type": period_type,
                            "period_end": period_end,
                            "statement": statement,
                            "line_item": str(line_item),
                            "value": float(value) if value is not None and value == value else None,
                        }
                    )
    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "period_type": pl.String,
                "period_end": pl.Date,
                "statement": pl.String,
                "line_item": pl.String,
                "value": pl.Float64,
            }
        )
    return pl.DataFrame(rows)


def upsert_financials(df: pl.DataFrame) -> int:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import FinancialStatementRow

    if df.is_empty():
        return 0

    with SessionLocal() as session:
        for row in df.iter_rows(named=True):
            stmt = sqlite_insert(FinancialStatementRow).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    FinancialStatementRow.symbol,
                    FinancialStatementRow.period_type,
                    FinancialStatementRow.period_end,
                    FinancialStatementRow.statement,
                    FinancialStatementRow.line_item,
                ],
                set_={"value": stmt.excluded.value},
            )
            session.execute(stmt)
        session.commit()
    return df.height


def fetch_security_info(symbol: str) -> dict | None:
    """Fetch sector/industry/shares outstanding via yfinance info."""
    tk = yf.Ticker(f"{symbol}.NS")
    try:
        info = tk.info
    except Exception as e:
        logger.warning("info %s: %s", symbol, e)
        return None
    if not info:
        return None
    shares = info.get("sharesOutstanding")
    if not any([info.get("sector"), info.get("industry"), shares]):
        return None
    return {
        "symbol": symbol,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "shares_outstanding": float(shares) if shares else None,
        "updated_at": datetime.now(UTC),
    }


def upsert_security_info(rows: list[dict]) -> int:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import SecurityInfo

    if not rows:
        return 0

    with SessionLocal() as session:
        for row in rows:
            stmt = sqlite_insert(SecurityInfo).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[SecurityInfo.symbol],
                set_={
                    "sector": stmt.excluded.sector,
                    "industry": stmt.excluded.industry,
                    "shares_outstanding": stmt.excluded.shares_outstanding,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            session.execute(stmt)
        session.commit()
    return len(rows)


def fill_gaps_for_symbol(symbol: str, start: date, end: date, rate: float = 0.5) -> int:
    """Backfill days missing in DuckDB with raw yfinance data (no overwrite)."""
    session = RateLimitedSession(min_interval=rate)
    yf_df = fetch_history(symbol, start, end)
    if yf_df.is_empty():
        return 0
    existing = market_db.fetch_ohlcv(symbol, start=str(start), end=str(end), limit=1_000_000)
    if not existing.is_empty():
        existing_days = existing.select(pl.col("ts").cast(pl.Date).unique())
        yf_df = yf_df.join(
            existing_days,
            left_on=pl.col("ts").cast(pl.Date),
            right_on="ts",
            how="anti",
        )
    if yf_df.is_empty():
        return 0
    return market_db.insert_ohlcv(yf_df)
