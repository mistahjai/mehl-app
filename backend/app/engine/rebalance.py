from dataclasses import dataclass, field
from datetime import date

from app.engine.wealth import LTCG_RATE, LONG_TERM_DAYS, STCG_RATE


@dataclass
class RebalanceTarget:
    symbol: str
    target_pct: float


@dataclass
class RebalanceRules:
    targets: list[RebalanceTarget] = field(default_factory=list)
    drift_band_pct: float = 5.0


@dataclass
class RebalanceSuggestion:
    symbol: str
    action: str  # buy / sell / hold
    current_pct: float
    target_pct: float
    drift_pct: float
    current_value: float
    target_value: float
    amount: float
    tax_est: float = 0.0


@dataclass
class RebalanceResult:
    total_value: float
    suggestions: list[RebalanceSuggestion] = field(default_factory=list)
    untaxed_symbols: list[str] = field(default_factory=list)

    @property
    def tax_total_est(self) -> float:
        return sum(s.tax_est for s in self.suggestions)


def suggest_rebalance(
    positions: list[dict],
    rules: RebalanceRules,
    price_by_symbol: dict[str, float],
    transactions_by_symbol: dict[str, list] | None = None,
) -> RebalanceResult:
    """Tax-aware rebalancing suggestions.

    Fresh purchases (buys) are always tax-free; sells are estimated with FIFO
    lots when transaction data is available. Symbols not in the target list
    drift to 0% and are suggested as full sells beyond the band.
    """
    result = RebalanceResult(total_value=0.0)

    valued: list[tuple[str, float]] = []
    for pos in positions:
        symbol = pos["symbol"]
        close = price_by_symbol.get(symbol)
        value = pos["quantity"] * close if close is not None else 0.0
        valued.append((symbol, value))
        result.total_value += value

    if result.total_value <= 0:
        return result

    targets = {t.symbol: t.target_pct for t in rules.targets}
    txns_by_symbol = transactions_by_symbol or {}

    def tax_for_sale(symbol: str, sale_value: float, sale_price: float) -> tuple[float, bool]:
        if sale_price <= 0:
            return (0.0, False)
        quantity = sale_value / sale_price
        lots: list[list] = []
        for t in sorted(
            txns_by_symbol.get(symbol, []), key=lambda t: (t.trade_date, t.id)
        ):
            if t.side == "buy":
                lots.append([t.trade_date, t.quantity, t.price, t.fees])
        if not lots:
            return (0.0, False)
        remaining = quantity
        lt_gain = 0.0
        st_gain = 0.0
        for lot in lots:
            if remaining <= 1e-9:
                break
            if lot[1] <= 1e-9:
                continue
            take = min(remaining, lot[1])
            cost_share = lot[2] + (lot[3] / lot[1] if lot[1] > 0 else 0.0)
            gain = (sale_price - cost_share) * take
            if (date.today() - lot[0]).days > LONG_TERM_DAYS:
                lt_gain += gain
            else:
                st_gain += gain
            lot[1] -= take
            remaining -= take
        # full Rs 1.25L exemption assumed available for this financial year
        tax = max(0.0, lt_gain) * LTCG_RATE + max(0.0, st_gain) * STCG_RATE
        return (tax, True)

    suggestions: list[RebalanceSuggestion] = []
    seen = set()
    for symbol, value in valued:
        seen.add(symbol)
        current_pct = value / result.total_value * 100.0
        target_pct = targets.get(symbol, 0.0)
        target_value = target_pct / 100.0 * result.total_value
        drift_value = value - target_value
        drift_pct = current_pct - target_pct
        action = "hold"
        amount = 0.0
        tax_est = 0.0
        untaxed = False
        if drift_value > 0 and (abs(drift_pct) > rules.drift_band_pct or target_pct == 0.0):
            action = "sell"
            amount = drift_value
            tax_est, has_lots = tax_for_sale(symbol, amount, price_by_symbol.get(symbol, 0.0))
            untaxed = not has_lots
        elif drift_value < 0 and abs(drift_pct) > rules.drift_band_pct:
            action = "buy"
            amount = -drift_value
        suggestions.append(
            RebalanceSuggestion(
                symbol=symbol,
                action=action,
                current_pct=round(current_pct, 2),
                target_pct=target_pct,
                drift_pct=round(drift_pct, 2),
                current_value=round(value, 2),
                target_value=round(target_value, 2),
                amount=round(amount, 2),
                tax_est=round(tax_est, 2),
            )
        )
        if untaxed:
            result.untaxed_symbols.append(symbol)

    # symbols in targets but not held: fresh buys
    for symbol, target_pct in targets.items():
        if symbol in seen:
            continue
        target_value = target_pct / 100.0 * result.total_value
        suggestions.append(
            RebalanceSuggestion(
                symbol=symbol,
                action="buy" if target_value > 0 else "hold",
                current_pct=0.0,
                target_pct=target_pct,
                drift_pct=-target_pct,
                current_value=0.0,
                target_value=round(target_value, 2),
                amount=round(target_value, 2),
                tax_est=0.0,
            )
        )

    result.suggestions = sorted(suggestions, key=lambda s: -abs(s.drift_pct))
    return result
