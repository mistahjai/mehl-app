from dataclasses import dataclass, field

import polars as pl


@dataclass
class BacktestConfig:
    symbol: str
    fast: int = 20
    slow: int = 50
    initial_capital: float = 10_000.0


@dataclass
class BacktestResult:
    symbol: str
    trades: int
    final_equity: float
    total_return_pct: float
    equity_curve: list[dict] = field(default_factory=list)


def run_backtest(df: pl.DataFrame, config: BacktestConfig) -> BacktestResult:
    if df.is_empty():
        return BacktestResult(config.symbol, 0, config.initial_capital, 0.0)
    if config.fast >= config.slow:
        raise ValueError("fast window must be smaller than slow window")

    df = (
        df.sort("ts")
        .with_columns(
            pl.col("close").rolling_mean(config.fast).alias("fast_ma"),
            pl.col("close").rolling_mean(config.slow).alias("slow_ma"),
        )
        .with_columns(
            pl.when(pl.col("fast_ma") > pl.col("slow_ma"))
            .then(1.0)
            .otherwise(0.0)
            .alias("target")
        )
        .with_columns(pl.col("target").shift(1).fill_null(0.0).alias("position"))
        .with_columns(
            (pl.col("close").pct_change().fill_null(0.0) * pl.col("position")).alias(
                "strategy_return"
            )
        )
        .with_columns(
            (1.0 + pl.col("strategy_return")).cum_prod().mul(config.initial_capital).alias(
                "equity"
            )
        )
    )

    trades = int(df.select((pl.col("position").diff().abs() > 0).sum()).item() or 0)
    final_equity = float(df["equity"][-1])
    total_return_pct = (final_equity / config.initial_capital - 1.0) * 100.0
    equity_curve = df.select(
        pl.col("ts").dt.to_string("%Y-%m-%dT%H:%M:%SZ").alias("time"), "equity"
    ).to_dicts()

    return BacktestResult(
        symbol=config.symbol,
        trades=trades,
        final_equity=final_equity,
        total_return_pct=total_return_pct,
        equity_curve=equity_curve,
    )
