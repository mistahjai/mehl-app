from dataclasses import dataclass, field
from datetime import date

import polars as pl

from app.engine.wealth import xirr

RISK_FREE = 0.07
REBALANCE_INTERVALS = {"monthly": 1, "quarterly": 3, "yearly": 12}


@dataclass
class AllocationConfig:
    symbols: list[str]
    weights: list[float] | None = None
    start: date = field(default_factory=lambda: date.today().replace(year=date.today().year - 10))
    end: date = field(default_factory=date.today)
    initial_capital: float = 100_000.0
    monthly_contribution: float = 0.0
    annual_step_up_pct: float = 0.0
    rebalance_freq: str | None = None
    benchmark: str = "NIFTY_50"

    def normalized_weights(self) -> list[float]:
        n = len(self.symbols)
        if not self.weights or len(self.weights) != n:
            return [1.0 / n] * n
        total = sum(self.weights)
        if total <= 0:
            return [1.0 / n] * n
        return [w / total for w in self.weights]


def monthly_close_matrix(daily: pl.DataFrame) -> pl.DataFrame:
    """Pivot month-end closes: one row per month (Date), one column per symbol."""
    df = daily.with_columns(
        pl.col("ts").cast(pl.Datetime).dt.truncate("1mo").alias("month")
    )
    monthly = (
        df.group_by("symbol", "month")
        .agg(pl.col("close").last().alias("close"))
        .sort("month")
    )
    piv = monthly.pivot(values="close", index="month", on="symbol")
    return piv.sort("month").with_columns(pl.col("month").cast(pl.Date))


def simulate_allocation(
    prices: pl.DataFrame,
    cfg: AllocationConfig,
    cashflows: list[tuple[date, float]] | None = None,
) -> dict:
    """Monthly allocation backtest over a month-close price matrix.

    prices: pivot from monthly_close_matrix (columns symbol, month, symbol...).
    Contributions are invested at each month-end per target weights; symbols
    with no price yet stay uninvested until data appears. Rebalancing
    normalizes target weights over currently-priced symbols. Returns
    {"curve": [...], "cashflows": [...]}.
    """
    symbols = [s for s in cfg.symbols if s in prices.columns]
    if not symbols:
        return {"curve": [], "cashflows": []}
    weights = dict(zip(cfg.symbols, cfg.normalized_weights()))
    weights = {s: weights[s] for s in symbols}

    rebalance_every = REBALANCE_INTERVALS.get(cfg.rebalance_freq or "", 0)
    rows = prices.iter_rows(named=True)
    values = {s: 0.0 for s in symbols}
    prev_price = {s: None for s in symbols}
    contribution = cfg.monthly_contribution
    invested = 0.0
    deployed_initial = False
    month_index = 0
    curve: list[dict] = []

    for row in rows:
        month_index += 1
        # 1. grow existing holdings with each symbol's own month-over-month return
        for s in symbols:
            price = row.get(s)
            if price is None:
                continue
            if prev_price[s] is not None and prev_price[s] > 0 and values[s] > 0:
                values[s] *= price / prev_price[s]
            prev_price[s] = price

        # 2. deploy initial capital at the first month with any price
        if not deployed_initial and any(prev_price[s] is not None for s in symbols):
            available = [s for s in symbols if prev_price[s] is not None]
            w_total = sum(weights[s] for s in available)
            if w_total > 0:
                for s in available:
                    values[s] += cfg.initial_capital * weights[s] / w_total
                invested += cfg.initial_capital
                deployed_initial = True
                if cashflows is not None and cfg.initial_capital > 0:
                    cashflows.append((row["month"].replace(day=1), -cfg.initial_capital))

        # 3. monthly contribution at each month-end, per target weights
        if contribution > 0:
            available = [s for s in symbols if prev_price[s] is not None]
            w_total = sum(weights[s] for s in available)
            if w_total > 0:
                for s in available:
                    values[s] += contribution * weights[s] / w_total
                invested += contribution
                if cashflows is not None:
                    cashflows.append((row["month"].replace(day=1), -contribution))
            if month_index % 12 == 0:
                contribution *= 1.0 + cfg.annual_step_up_pct

        # 4. rebalance back to target weights
        if rebalance_every and month_index % rebalance_every == 0:
            available = [s for s in symbols if prev_price[s] is not None and values[s] > 0]
            w_total = sum(weights[s] for s in available)
            if w_total > 0 and available:
                total_value = sum(values[s] for s in available)
                for s in available:
                    values[s] = total_value * weights[s] / w_total

        total = sum(values.values())
        curve.append(
            {
                "date": row["month"].isoformat(),
                "value": round(total, 2),
                "invested": round(invested, 2),
            }
        )

    if cashflows is not None and invested > 0:
        last = curve[-1]
        cashflows.append((date.fromisoformat(last["date"]), last["value"]))
    return {"curve": curve, "cashflows": cashflows or []}


def performance_metrics(curve: list[dict], cashflows: list[tuple[date, float]]) -> dict:
    """Headline metrics for a backtest curve (money-weighted where noted)."""
    if len(curve) < 2:
        return {}
    values = [row["value"] for row in curve]
    final_value = values[-1]
    total_invested = curve[-1]["invested"]

    monthly_returns = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev > 0 and values[i] != prev:
            monthly_returns.append(values[i] / prev - 1)
    vol = 0.0
    if len(monthly_returns) > 1:
        mean = sum(monthly_returns) / len(monthly_returns)
        var = sum((r - mean) ** 2 for r in monthly_returns) / (len(monthly_returns) - 1)
        vol = (var**0.5) * (12**0.5)

    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, 1.0 - v / peak)

    rate = xirr(cashflows)
    total_return_pct = (final_value / total_invested - 1.0) * 100.0 if total_invested > 0 else 0.0

    rolling: dict[str, dict] = {}
    for label, window in (("1y", 12), ("3y", 36), ("5y", 60)):
        rets = [
            values[i] / values[i - window] - 1.0
            for i in range(window, len(values))
            if values[i - window] > 0
        ]
        if rets:
            rolling[label] = {
                "min_pct": round(min(rets) * 100, 2),
                "max_pct": round(max(rets) * 100, 2),
                "latest_pct": round(rets[-1] * 100, 2),
            }

    return {
        "xirr_pct": round(rate * 100, 2) if rate is not None else None,
        "volatility_pct": round(vol * 100, 2),
        "sharpe": round((rate - RISK_FREE) / vol, 2) if rate is not None and vol > 0 else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "total_return_pct": round(total_return_pct, 2),
        "total_invested": round(total_invested, 2),
        "final_value": round(final_value, 2),
        "rolling_returns": rolling,
    }
