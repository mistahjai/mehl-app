from datetime import date, datetime

from pydantic import BaseModel, Field


class PortfolioCreate(BaseModel):
    name: str
    base_currency: str = "INR"
    rebalancing_rules: dict = Field(default_factory=dict)


class PortfolioRead(PortfolioCreate):
    id: int
    created_at: datetime


class TransactionCreate(BaseModel):
    trade_date: date
    symbol: str
    isin: str | None = None
    side: str = Field(pattern="^(buy|sell)$")
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = Field(default=0.0, ge=0)
    note: str | None = None


class TransactionRead(TransactionCreate):
    id: int
    portfolio_id: int
    created_at: datetime


class ComputedPosition(BaseModel):
    symbol: str
    isin: str | None = None
    quantity: float = 0.0
    avg_cost: float = 0.0
    invested: float = 0.0
    realized: float = 0.0


class HoldingSummary(ComputedPosition):
    close: float | None = None
    last_ts: str | None = None
    current_value: float | None = None
    unrealized: float | None = None
    unrealized_pct: float | None = None


class PortfolioSummary(BaseModel):
    holdings: list[HoldingSummary]
    invested: float = 0.0
    current_value: float = 0.0
    unrealized: float = 0.0
    realized: float = 0.0
    dividends: float = 0.0
    xirr: float | None = None


class DividendCreate(BaseModel):
    ex_date: date
    symbol: str
    isin: str | None = None
    amount_per_share: float | None = Field(default=None, ge=0)
    total_amount: float | None = Field(default=None, ge=0)
    note: str | None = None


class DividendRead(DividendCreate):
    id: int
    portfolio_id: int
    created_at: datetime


class WatchlistItemCreate(BaseModel):
    symbol: str
    note: str | None = None


class WatchlistItemRead(WatchlistItemCreate):
    id: int
    created_at: datetime


class BacktestRequest(BaseModel):
    symbol: str
    fast: int = 20
    slow: int = 50
    initial_capital: float = 10_000.0


class ScreenFilter(BaseModel):
    metric: str
    op: str = Field(pattern="^(lt|lte|gt|gte|eq|between)$")
    value: float | list[float]


class ScreenerRequest(BaseModel):
    filters: list[ScreenFilter] = Field(default_factory=list)
    rank_by: str | None = None
    rank_desc: bool = True
    limit: int = Field(default=100, ge=1, le=1000)
    instrument_types: list[str] | None = None


class ScreenCreate(BaseModel):
    name: str
    filters: list[ScreenFilter] = Field(default_factory=list)
    rank_by: str | None = None
    rank_desc: bool = True


class ScreenRead(ScreenCreate):
    id: int
    created_at: datetime


class ScenarioAsset(BaseModel):
    name: str
    weight: float = Field(gt=0)
    expected_return: float = Field(ge=-1, le=2)
    volatility: float = Field(default=0.0, ge=0, le=2)


class ScenarioParams(BaseModel):
    start_value: float = Field(default=0.0, ge=0)
    monthly_contribution: float = Field(default=10_000.0, ge=0)
    annual_step_up_pct: float = Field(default=0.0, ge=0, le=1)
    expected_return: float = Field(default=0.12, ge=-0.9, le=0.6)
    volatility: float = Field(default=0.15, ge=0, le=2)
    years: int = Field(default=20, ge=1, le=50)
    inflation: float = Field(default=0.06, ge=0, le=0.5)
    assets: list[ScenarioAsset] = Field(default_factory=list)


class ScenarioCreate(BaseModel):
    name: str
    params: ScenarioParams = Field(default_factory=ScenarioParams)


class ScenarioRead(ScenarioCreate):
    id: int
    created_at: datetime


class ScenarioRunRequest(BaseModel):
    params: ScenarioParams
    num_paths: int = Field(default=1000, ge=1, le=20_000)
    seed: int | None = None


class GoalRequest(BaseModel):
    target: float = Field(gt=0)
    years: int = Field(ge=1, le=50)
    expected_return: float = Field(default=0.12, ge=-0.9, le=0.6)
    annual_step_up_pct: float = Field(default=0.0, ge=0, le=1)
    inflation: float = Field(default=0.0, ge=0, le=0.5)


class AllocationBacktestRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    weights: list[float] | None = None
    start: date | None = None
    end: date | None = None
    initial_capital: float = Field(default=100_000.0, ge=0)
    monthly_contribution: float = Field(default=0.0, ge=0)
    annual_step_up_pct: float = Field(default=0.0, ge=0, le=1)
    rebalance_freq: str | None = Field(default=None, pattern="^(monthly|quarterly|yearly)$")
    benchmark: str = "NIFTY_50"


class RebalanceTarget(BaseModel):
    symbol: str
    target_pct: float = Field(ge=0, le=100)


class RebalanceRules(BaseModel):
    targets: list[RebalanceTarget] = Field(default_factory=list)
    drift_band_pct: float = Field(default=5.0, ge=0, le=100)


class RebalanceRequest(BaseModel):
    rules: RebalanceRules | None = None
    years: int = Field(default=10, ge=1, le=40)
