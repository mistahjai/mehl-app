import sqlite3
from datetime import date

import polars as pl

from app.config import settings

PRICE_COLS = ["open", "high", "low", "close"]

_EMPTY_ACTIONS_SCHEMA = {
    "symbol": pl.String,
    "ex_date": pl.Date,
    "factor": pl.Float64,
}


def load_actions(symbols: list[str] | None = None) -> pl.DataFrame:
    path = settings.data_dir / "mehl.db"
    if not path.exists():
        return pl.DataFrame(schema=_EMPTY_ACTIONS_SCHEMA)
    con = sqlite3.connect(path)
    try:
        query = "SELECT symbol, ex_date, factor FROM corporate_actions WHERE factor IS NOT NULL AND factor > 0"
        params: list[object] = []
        if symbols:
            query += f" AND symbol IN ({','.join('?' * len(symbols))})"
            params = list(symbols)
        cur = con.execute(query, params)
        rows = cur.fetchall()
    finally:
        con.close()
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.String, "ex_date": pl.String, "factor": pl.Float64},
        orient="row",
    ).with_columns(pl.col("ex_date").str.to_date("%Y-%m-%d"))


def apply_corporate_adjustments(df: pl.DataFrame, actions: pl.DataFrame) -> pl.DataFrame:
    """Adjust OHLC prices for splits and bonuses.

    Actions must have columns: symbol, ex_date (Date), factor (> 0). Prices
    strictly before each ex-date are divided by the factor so pre- and
    post-action prices are comparable. Multiple actions compound.
    """
    if df.is_empty() or actions.is_empty():
        return df

    a = actions.select(
        "symbol",
        "ex_date",
        (1.0 / pl.col("factor")).alias("adj"),
    )
    joined = df.join(a, on="symbol", how="left")
    has_action = pl.col("ex_date").is_not_null()
    before = has_action & (pl.col("ts").cast(pl.Date) < pl.col("ex_date"))
    joined = joined.with_columns(
        pl.when(before).then(pl.col("adj")).otherwise(1.0).alias("mult")
    )

    other_cols = [c for c in df.columns if c not in ("symbol", "ts")]
    out = joined.group_by("symbol", "ts").agg(
        pl.col("mult").product(),
        *[pl.col(c).first() for c in other_cols],
    )
    price_cols = [c for c in PRICE_COLS if c in out.columns]
    out = out.with_columns([(pl.col(c) * pl.col("mult")).alias(c) for c in price_cols])
    return out.select(df.columns).sort(["symbol", "ts"])


def adjustment_factor_for(action_type: str, old_value: float, new_value: float) -> float:
    if action_type == "split":
        if new_value <= 0:
            raise ValueError("new face value must be positive")
        return old_value / new_value
    if action_type == "bonus":
        if new_value <= 0:
            raise ValueError("bonus denominator must be positive")
        return (old_value + new_value) / new_value
    return 1.0


def latest_action_date(con: sqlite3.Connection) -> date | None:
    row = con.execute("SELECT MAX(ex_date) FROM corporate_actions").fetchone()
    if row is None or row[0] is None:
        return None
    return date.fromisoformat(row[0])
