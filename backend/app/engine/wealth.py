from dataclasses import dataclass, field
from datetime import date, datetime

import polars as pl


# Indian equity tax rules (post Jul 2024): LTCG 12.5% above Rs 1.25L
# exemption, STCG 20%. Equity-oriented MFs follow the same rates.
LTCG_EXEMPTION = 125_000.0
LTCG_RATE = 0.125
STCG_RATE = 0.20
LONG_TERM_DAYS = 365


@dataclass
class TaxLot:
    symbol: str
    buy_date: date
    sell_date: date
    quantity: float
    buy_price: float
    sell_price: float
    gain: float
    holding_days: int
    term: str


@dataclass
class GainsResult:
    ltcg: float = 0.0
    stcg: float = 0.0
    ltcg_tax: float = 0.0
    stcg_tax: float = 0.0
    lots: list[TaxLot] = field(default_factory=list)


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Money-weighted annualized return: grid scan for a sign change, then
    bisection. Returns None when flows are one-sided or no rate is found.
    """
    flows = [
        (d.date() if isinstance(d, datetime) else d, a)
        for d, a in cashflows
        if a != 0
    ]
    if len(flows) < 2:
        return None
    has_pos = any(a > 0 for _, a in flows)
    has_neg = any(a < 0 for _, a in flows)
    if not (has_pos and has_neg):
        return None
    d0 = min(d for d, _ in flows)

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** ((d - d0).days / 365.0) for d, a in flows)

    grid = [-0.9, -0.75, -0.5, -0.25, -0.1, 0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0, 50.0]
    prev_rate = grid[0]
    f_prev = npv(prev_rate)
    for rate in grid[1:]:
        f = npv(rate)
        if f_prev * f <= 0:
            lo, hi, f_lo = prev_rate, rate, f_prev
            for _ in range(200):
                mid = (lo + hi) / 2
                f_mid = npv(mid)
                if abs(f_mid) < 1e-7:
                    return mid
                if f_lo * f_mid < 0:
                    hi = mid
                else:
                    lo, f_lo = mid, f_mid
            return (lo + hi) / 2
        prev_rate, f_prev = rate, f
    return None


def fifo_lots(transactions: list) -> GainsResult:
    """Realized gains via FIFO lot matching, split into LT/ST buckets.

    transactions must expose trade_date, symbol, side, quantity, price, fees.
    """
    per_symbol: dict[str, list] = {}
    for txn in transactions:
        per_symbol.setdefault(txn.symbol, []).append(txn)

    result = GainsResult()
    for symbol, txns in per_symbol.items():
        txns = sorted(txns, key=lambda t: (t.trade_date, t.id))
        open_lots: list[list] = []  # [date, qty_remaining, price, fees]
        for txn in txns:
            if txn.side == "buy":
                open_lots.append([txn.trade_date, txn.quantity, txn.price, txn.fees])
                continue
            remaining = txn.quantity
            while remaining > 1e-9 and open_lots:
                lot = open_lots[0]
                take = min(remaining, lot[1])
                if take <= 1e-9:
                    open_lots.pop(0)
                    continue
                cost_share = lot[2] + (lot[3] / lot[1] if lot[1] > 0 else 0.0)
                gain = (txn.price - cost_share) * take
                holding_days = (txn.trade_date - lot[0]).days
                term = "lt" if holding_days > LONG_TERM_DAYS else "st"
                result.lots.append(
                    TaxLot(
                        symbol=symbol,
                        buy_date=lot[0],
                        sell_date=txn.trade_date,
                        quantity=take,
                        buy_price=cost_share,
                        sell_price=txn.price,
                        gain=gain,
                        holding_days=holding_days,
                        term=term,
                    )
                )
                if term == "lt":
                    result.ltcg += gain
                else:
                    result.stcg += gain
                lot[1] -= take
                remaining -= take
                if lot[1] <= 1e-9:
                    open_lots.pop(0)

    result.ltcg_tax = max(0.0, result.ltcg - LTCG_EXEMPTION) * LTCG_RATE
    result.stcg_tax = max(0.0, result.stcg) * STCG_RATE
    return result


def _daily_holdings(txns: list) -> pl.DataFrame:
    """Cumulative held quantity and cash flow per trade date for one symbol."""
    deltas = pl.DataFrame(
        {
            "date": [t.trade_date for t in txns],
            "qty_delta": [t.quantity if t.side == "buy" else -t.quantity for t in txns],
            "cash_delta": [
                -(t.quantity * t.price + t.fees) if t.side == "buy" else t.quantity * t.price
                for t in txns
            ],
        }
    )
    return (
        deltas.group_by("date")
        .agg(pl.col("qty_delta").sum(), pl.col("cash_delta").sum())
        .sort("date")
        .with_columns(
            pl.col("qty_delta").cum_sum().alias("held"),
            (-pl.col("cash_delta").cum_sum()).alias("invested"),
        )
    )


def equity_curve(transactions: list, prices: dict[str, pl.DataFrame]) -> list[dict]:
    """Daily portfolio value from transactions + historical closes.

    prices maps symbol -> DataFrame with (ts, close). Value on a day uses the
    last available close on or before that day per symbol (as-of join).
    """
    per_symbol: dict[str, list] = {}
    for txn in transactions:
        per_symbol.setdefault(txn.symbol, []).append(txn)

    frames: list[pl.DataFrame] = []
    invested_frames: list[pl.DataFrame] = []
    for symbol, txns in per_symbol.items():
        price_df = prices.get(symbol)
        if price_df is None or price_df.is_empty():
            continue
        holdings = _daily_holdings(txns)
        p = price_df.select(
            pl.col("ts").cast(pl.Date).alias("date"), pl.col("close")
        ).sort("date")
        joined = p.join_asof(holdings, left_on="date", right_on="date", strategy="backward")
        joined = joined.with_columns(
            (pl.col("held").fill_null(0.0) * pl.col("close")).alias("value")
        ).filter(pl.col("held").is_not_null() & (pl.col("held") > 1e-9))
        frames.append(joined.select("date", "value"))
        invested_frames.append(joined.select("date", "invested"))

    if not frames:
        return []

    values = pl.concat(frames).group_by("date").agg(pl.col("value").sum()).sort("date")
    invested = pl.concat(invested_frames).group_by("date").agg(pl.col("invested").sum()).sort("date")
    out = values.join(invested, on="date", how="left")
    return out.select(
        pl.col("date").dt.to_string("%Y-%m-%d").alias("date"), "value", "invested"
    ).to_dicts()


def dividend_income(transactions: list, dividends: list) -> float:
    """Total dividend income, deriving totals from per-share amounts when absent."""
    total = 0.0
    for div in dividends:
        if div.total_amount is not None:
            total += div.total_amount
            continue
        if div.amount_per_share is None:
            continue
        held = held_quantity_at(transactions, div.symbol, div.ex_date)
        total += div.amount_per_share * held
    return total


def held_quantity_at(transactions: list, symbol: str, on_date: date) -> float:
    held = 0.0
    for t in transactions:
        if t.symbol != symbol or t.trade_date > on_date:
            continue
        held += t.quantity if t.side == "buy" else -t.quantity
    return max(0.0, held)
