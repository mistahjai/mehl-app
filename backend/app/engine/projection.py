import numpy as np
from dataclasses import dataclass, field, replace
from datetime import date, timedelta


@dataclass
class ProjectionConfig:
    start_value: float = 0.0
    monthly_contribution: float = 10_000.0
    annual_step_up_pct: float = 0.0
    expected_return: float = 0.12
    volatility: float = 0.15
    years: int = 20
    inflation: float = 0.06
    assets: list[dict] = field(default_factory=list)  # {name, weight, expected_return, volatility}

    def blended(self) -> "ProjectionConfig":
        """Blend expected return/volatility from an asset allocation list.

        Weights are normalized; correlation is assumed zero (documented
        simplification), so volatility is the weighted sum.
        """
        if not self.assets:
            return self
        total_weight = sum(a.get("weight", 0) for a in self.assets)
        if total_weight <= 0:
            return self
        ret = sum(a.get("expected_return", 0) * a["weight"] for a in self.assets) / total_weight
        vol = sum(a.get("volatility", 0) * a["weight"] for a in self.assets) / total_weight
        return ProjectionConfig(
            start_value=self.start_value,
            monthly_contribution=self.monthly_contribution,
            annual_step_up_pct=self.annual_step_up_pct,
            expected_return=ret,
            volatility=vol,
            years=self.years,
            inflation=self.inflation,
            assets=self.assets,
        )

    def months(self) -> int:
        return self.years * 12


def _monthly_rate(annual: float) -> float:
    """Geometric monthly rate: median of simulated paths matches the
    deterministic projection at the same annual assumption."""
    return (1.0 + annual) ** (1.0 / 12.0) - 1.0


def project(cfg: ProjectionConfig) -> list[dict]:
    """Deterministic month-by-month projection with SIP + annual step-up.

    Returns monthly rows: {month, date, value, invested, real_value}.
    """
    cfg = cfg.blended()
    r = _monthly_rate(cfg.expected_return)
    infl = _monthly_rate(cfg.inflation) if cfg.inflation > 0 else 0.0
    value = cfg.start_value
    contribution = cfg.monthly_contribution
    invested = cfg.start_value
    start = date.today().replace(day=1)
    rows: list[dict] = []
    for month in range(1, cfg.months() + 1):
        value = value * (1.0 + r) + contribution
        invested += contribution
        if month % 12 == 0:
            contribution *= 1.0 + cfg.annual_step_up_pct
        rows.append(
            {
                "month": month,
                "date": (start + timedelta(days=30 * month)).isoformat(),
                "value": round(value, 2),
                "invested": round(invested, 2),
                "real_value": round(value / (1.0 + infl) ** month, 2) if infl else round(value, 2),
            }
        )
    return rows


def monte_carlo(
    cfg: ProjectionConfig,
    num_paths: int = 1000,
    seed: int | None = None,
    percentiles: tuple[float, ...] = (0.1, 0.5, 0.9),
) -> dict:
    """Simulate paths with monthly returns ~ N(geometric monthly, vol/sqrt(12)).

    Returns {"paths": num_paths, "final_percentiles": {p: value},
    "bands": [{month, p10, p50, p90, ...}]} for the given percentiles.
    """
    cfg = cfg.blended()
    if cfg.months() < 1 or num_paths < 1:
        return {"paths": 0, "final_percentiles": {}, "bands": []}
    rng = np.random.default_rng(seed)
    g = _monthly_rate(cfg.expected_return)
    sigma = cfg.volatility / np.sqrt(12.0)
    # Shift the mean up by sigma^2/2 so E[log(1+r)] = log(1+g): the median
    # simulated path then matches the deterministic projection (volatility
    # drag otherwise pulls the median ~e^(-sigma^2*t/2) below it).
    mu = g + sigma * sigma / 2.0
    months = cfg.months()

    draws = rng.normal(mu, sigma, size=(num_paths, months))
    growth = np.cumprod(1.0 + draws, axis=1)
    if cfg.annual_step_up_pct > 0:
        contributions = np.full(months, cfg.monthly_contribution, dtype=np.float64)
        for m in range(11, months, 12):
            contributions[m + 1 :] = contributions[m] * (1.0 + cfg.annual_step_up_pct)
    else:
        contributions = np.full(months, cfg.monthly_contribution, dtype=np.float64)
    # value_m = start * G[m] + G[m] * sum_i(c_i / G[i]), G = cumulative growth
    values = cfg.start_value * growth + growth * np.cumsum(contributions / growth, axis=1)

    bands: list[dict] = []
    for m in range(months):
        row: dict = {"month": m + 1}
        for p in percentiles:
            row[f"p{int(p * 100)}"] = round(float(np.percentile(values[:, m], p * 100)), 2)
        bands.append(row)

    final = values[:, -1]
    final_percentiles = {
        f"p{int(p * 100)}": round(float(np.percentile(final, p * 100)), 2) for p in percentiles
    }
    return {
        "paths": num_paths,
        "expected_return": round(cfg.expected_return, 4),
        "final_percentiles": final_percentiles,
        "bands": bands,
    }


def required_monthly_contribution(
    target: float,
    cfg: ProjectionConfig,
    tolerance: float = 1.0,
) -> float | None:
    """Monthly SIP needed to reach `target` by the end of the horizon.

    Bisection over the contribution amount, reusing the deterministic
    simulator so step-up and blended returns are honored.
    """
    if target <= 0:
        return None
    lo, hi = 0.0, max(target / (cfg.months() / 12.0), 100.0)
    # grow hi until the projection overshoots
    for _ in range(60):
        final = project(replace(cfg, monthly_contribution=hi))[-1]["value"]
        if final >= target:
            break
        hi *= 2.0
    else:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        final = project(replace(cfg, monthly_contribution=mid))[-1]["value"]
        if abs(final - target) <= tolerance:
            return round(mid, 2)
        if final < target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 2)
