export interface OhlcvRow {
  symbol: string;
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface IndexRow {
  symbol: string;
  ts: string;
  close: number;
}

export interface IndexSummary {
  symbol: string;
  rows: number;
  first_ts: string;
  last_ts: string;
  last_close: number;
  is_live: boolean;
  is_usable: boolean;
}

export interface Transaction {
  id: number;
  portfolio_id: number;
  trade_date: string;
  symbol: string;
  isin: string | null;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  fees: number;
  note: string | null;
  created_at: string;
}

export interface ComputedPosition {
  symbol: string;
  isin: string | null;
  quantity: number;
  avg_cost: number;
  invested: number;
}

export interface Dividend {
  id: number;
  portfolio_id: number;
  ex_date: string;
  symbol: string;
  isin: string | null;
  amount_per_share: number | null;
  total_amount: number | null;
  note: string | null;
  created_at: string;
}

export interface Portfolio {
  id: number;
  name: string;
  base_currency: string;
  rebalancing_rules: Record<string, unknown>;
  created_at: string;
}

export interface WatchlistItem {
  id: number;
  symbol: string;
  note: string | null;
  created_at: string;
}

export interface ScreenFilter {
  metric: string;
  op: "lt" | "lte" | "gt" | "gte" | "eq" | "between";
  value: number | [number, number];
}

export interface ScreenerResponse {
  total: number;
  rows: ScreenerRow[];
}

export interface ScreenerRow {
  symbol: string;
  name: string | null;
  sector: string | null;
  instrument_type: string | null;
  close: number | null;
  volume: number | null;
  market_cap: number | null;
  pe: number | null;
  pb: number | null;
  roe: number | null;
  roce: number | null;
  debt_to_equity: number | null;
  net_margin: number | null;
  operating_margin: number | null;
  revenue_growth: number | null;
  earnings_growth: number | null;
}

export interface FundamentalsResponse {
  symbol: string;
  latest: Record<string, number | string | null> | null;
  history: {
    period_end: string;
    period_type: string;
    pe: number | null;
    pb: number | null;
    roe: number | null;
    roce: number | null;
    debt_to_equity: number | null;
    net_margin: number | null;
    operating_margin: number | null;
  }[];
}

export interface StatementsResponse {
  symbol: string;
  statements: Record<
    string,
    { period_end: string; period_type: string; line_item: string; value: number | null }[]
  >;
}

export interface SavedScreen {
  id: number;
  name: string;
  filters: ScreenFilter[];
  rank_by: string | null;
  rank_desc: boolean;
  created_at: string;
}

export interface HoldingSummary {
  symbol: string;
  isin: string | null;
  quantity: number;
  avg_cost: number;
  invested: number;
  realized: number;
  close: number | null;
  last_ts: string | null;
  current_value: number | null;
  unrealized: number | null;
  unrealized_pct: number | null;
}

export interface PortfolioSummary {
  holdings: HoldingSummary[];
  invested: number;
  current_value: number;
  unrealized: number;
  realized: number;
  dividends: number;
  xirr: number | null;
}

export interface GainsResponse {
  ltcg: number;
  stcg: number;
  ltcg_tax_est: number;
  stcg_tax_est: number;
  ltcg_exemption: number;
  ltcg_rate: number;
  stcg_rate: number;
  lots: {
    symbol: string;
    buy_date: string;
    sell_date: string;
    quantity: number;
    buy_price: number;
    sell_price: number;
    gain: number;
    holding_days: number;
    term: "lt" | "st";
  }[];
}

export interface EquityCurvePoint {
  date: string;
  value: number;
  invested: number;
}

export interface ScenarioAsset {
  name: string;
  weight: number;
  expected_return: number;
  volatility: number;
}

export interface ScenarioParams {
  start_value: number;
  monthly_contribution: number;
  annual_step_up_pct: number;
  expected_return: number;
  volatility: number;
  years: number;
  inflation: number;
  assets: ScenarioAsset[];
}

export interface SavedScenario {
  id: number;
  name: string;
  params: ScenarioParams;
  created_at: string;
}

export interface ScenarioRunResult {
  projection: { month: number; date: string; value: number; invested: number; real_value: number }[];
  monte_carlo: {
    paths: number;
    expected_return: number;
    final_percentiles: Record<string, number>;
    bands: Record<string, number>[];
  };
}

export const defaultScenarioParams = (): ScenarioParams => ({
  start_value: 0,
  monthly_contribution: 25000,
  annual_step_up_pct: 0.08,
  expected_return: 0.12,
  volatility: 0.15,
  years: 25,
  inflation: 0.05,
  assets: [],
});

export interface BacktestMetrics {
  xirr_pct: number | null;
  volatility_pct: number | null;
  sharpe: number | null;
  max_drawdown_pct: number | null;
  total_return_pct: number | null;
  total_invested: number | null;
  final_value: number | null;
  rolling_returns: Record<string, { min_pct: number; max_pct: number; latest_pct: number }>;
}

export interface AllocationBacktestResult {
  symbols: string[];
  missing_symbols: string[];
  strategy: { curve: { date: string; value: number; invested: number }[]; metrics: BacktestMetrics };
  benchmark: {
    symbol: string;
    curve: { date: string; value: number; invested: number }[];
    metrics: BacktestMetrics;
  } | null;
}

export function backtestAllocation(body: {
  symbols: string[];
  weights?: number[] | null;
  start?: string | null;
  end?: string | null;
  initial_capital?: number;
  monthly_contribution?: number;
  annual_step_up_pct?: number;
  rebalance_freq?: string | null;
  benchmark?: string;
}): Promise<AllocationBacktestResult> {
  return postJson<AllocationBacktestResult>("/api/market/backtest-allocation", body);
}

export interface RebalanceTarget {
  symbol: string;
  target_pct: number;
}

export interface RebalanceRules {
  targets: RebalanceTarget[];
  drift_band_pct: number;
}

export interface RebalanceSuggestion {
  symbol: string;
  action: "buy" | "sell" | "hold";
  current_pct: number;
  target_pct: number;
  drift_pct: number;
  current_value: number;
  target_value: number;
  amount: number;
  tax_est: number;
}

export interface RebalanceResult {
  total_value: number;
  suggestions: RebalanceSuggestion[];
  tax_total_est: number;
  untaxed_symbols: string[];
  drift_band_pct: number;
  projection: {
    years: number;
    blended_return_pct: number | null;
    projected_value: number | null;
  };
}

export function saveRebalanceRules(portfolioId: number, rules: RebalanceRules): Promise<Portfolio> {
  return fetch(`/api/portfolios/${portfolioId}/rules`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rules),
  }).then((res) => {
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json() as Promise<Portfolio>;
  });
}

export function runRebalance(
  portfolioId: number,
  body: { rules?: RebalanceRules | null; years?: number } = {},
): Promise<RebalanceResult> {
  return postJson<RebalanceResult>(`/api/portfolios/${portfolioId}/rebalance`, body);
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export function fetchPortfolios(): Promise<Portfolio[]> {
  return get<Portfolio[]>("/api/portfolios");
}

export function fetchOhlcv(symbol: string, limit = 5000): Promise<OhlcvRow[]> {
  return get<OhlcvRow[]>(`/api/market/ohlcv/${encodeURIComponent(symbol)}?limit=${limit}`);
}

export function fetchIndices(symbol: string): Promise<IndexRow[]> {
  return get<IndexRow[]>(`/api/market/indices/${encodeURIComponent(symbol)}`);
}

export function fetchIndexSummaries(): Promise<IndexSummary[]> {
  return get<IndexSummary[]>("/api/market/indices");
}

export function fetchPositions(portfolioId: number): Promise<ComputedPosition[]> {
  return get<ComputedPosition[]>(`/api/portfolios/${portfolioId}/positions`);
}

export function fetchTransactions(portfolioId: number): Promise<Transaction[]> {
  return get<Transaction[]>(`/api/portfolios/${portfolioId}/transactions`);
}

export function fetchDividends(portfolioId: number): Promise<Dividend[]> {
  return get<Dividend[]>(`/api/portfolios/${portfolioId}/dividends`);
}

export function fetchWatchlist(): Promise<WatchlistItem[]> {
  return get<WatchlistItem[]>("/api/watchlist");
}

export async function runScreener(body: {
  filters: ScreenFilter[];
  rank_by?: string | null;
  rank_desc?: boolean;
  limit?: number;
  instrument_types?: string[] | null;
}): Promise<ScreenerResponse> {
  const res = await fetch("/api/market/screener", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as ScreenerResponse;
}

export function fetchFundamentals(symbol: string): Promise<FundamentalsResponse> {
  return get<FundamentalsResponse>(`/api/market/fundamentals/${encodeURIComponent(symbol)}`);
}

export function fetchStatements(symbol: string): Promise<StatementsResponse> {
  return get<StatementsResponse>(`/api/market/fundamentals/${encodeURIComponent(symbol)}/statements`);
}

export function fetchSavedScreens(): Promise<SavedScreen[]> {
  return get<SavedScreen[]>("/api/screens");
}

export function fetchSummary(portfolioId: number): Promise<PortfolioSummary> {
  return get<PortfolioSummary>(`/api/portfolios/${portfolioId}/summary`);
}

export function fetchGains(portfolioId: number): Promise<GainsResponse> {
  return get<GainsResponse>(`/api/portfolios/${portfolioId}/gains`);
}

export function fetchEquityCurve(portfolioId: number): Promise<EquityCurvePoint[]> {
  return get<EquityCurvePoint[]>(`/api/portfolios/${portfolioId}/equity-curve`);
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export function createPortfolio(name: string): Promise<Portfolio> {
  return postJson<Portfolio>("/api/portfolios", { name });
}

export type TransactionInput = Omit<
  Transaction,
  "id" | "portfolio_id" | "created_at" | "isin" | "note"
> & {
  isin?: string | null;
  note?: string | null;
};

export type DividendInput = Omit<
  Dividend,
  "id" | "portfolio_id" | "created_at" | "isin" | "note"
> & {
  isin?: string | null;
  note?: string | null;
};

export function addTransaction(
  portfolioId: number,
  txn: TransactionInput,
): Promise<Transaction> {
  return postJson<Transaction>(`/api/portfolios/${portfolioId}/transactions`, txn);
}

export function addDividend(
  portfolioId: number,
  dividend: DividendInput,
): Promise<Dividend> {
  return postJson<Dividend>(`/api/portfolios/${portfolioId}/dividends`, dividend);
}

export async function deleteResource(path: string): Promise<void> {
  const res = await fetch(path, { method: "DELETE" });
  if (!res.ok && res.status !== 204) throw new Error(`${res.status} ${res.statusText}`);
}

export function fetchScenarios(): Promise<SavedScenario[]> {
  return get<SavedScenario[]>("/api/scenarios");
}

export function fetchScenario(id: number): Promise<SavedScenario> {
  return get<SavedScenario>(`/api/scenarios/${id}`);
}

export function runScenario(id: number, numPaths = 1000): Promise<ScenarioRunResult> {
  return postJson<ScenarioRunResult>(`/api/scenarios/${id}/run?num_paths=${numPaths}`, {});
}

export function previewScenario(params: ScenarioParams, numPaths = 1000): Promise<ScenarioRunResult> {
  return postJson<ScenarioRunResult>("/api/scenarios/preview", { params, num_paths: numPaths });
}

export function saveScenario(name: string, params: ScenarioParams): Promise<SavedScenario> {
  return postJson<SavedScenario>("/api/scenarios", { name, params });
}

export function updateScenario(id: number, name: string, params: ScenarioParams): Promise<SavedScenario> {
  return fetch(`/api/scenarios/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, params }),
  }).then((res) => {
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json() as Promise<SavedScenario>;
  });
}

export function goalPlan(body: {
  target: number;
  years: number;
  expected_return: number;
  annual_step_up_pct?: number;
  inflation?: number;
}): Promise<{ target: number; years: number; required_monthly_contribution: number | null; projected_value: number }> {
  return postJson("/api/scenarios/goal", body);
}

export type TickerType = "stock" | "etf" | "mf" | "index";

export interface Ticker {
  symbol: string;
  name: string | null;
  type: TickerType;
  is_active: boolean;
  date_first_entry: string | null;
  date_last_entry: string | null;
}

export interface OverviewResponse {
  symbol: string;
  name: string | null;
  type: TickerType;
  price: number | null;
  last_ts: string | null;
  returns: Record<string, number | null>;
  metrics: Record<string, number | null>;
}

export function searchTickers(q: string, types?: string | null, limit = 12): Promise<Ticker[]> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  if (types) params.set("types", types);
  return get<Ticker[]>(`/api/market/tickers?${params.toString()}`);
}

export function fetchOverview(symbol: string): Promise<OverviewResponse> {
  return get<OverviewResponse>(`/api/market/overview/${encodeURIComponent(symbol)}`);
}

export function fetchCloseOn(
  symbol: string,
  day: string,
): Promise<{ symbol: string; day: string; close: number | null }> {
  return get(
    `/api/market/close-on?symbol=${encodeURIComponent(symbol)}&day=${encodeURIComponent(day)}`,
  );
}

export interface ImportResult {
  imported: number;
  total: number;
  skipped: { row: number; reason: string }[];
}

export function importTransactionsCsv(portfolioId: number, file: File): Promise<ImportResult> {
  const form = new FormData();
  form.append("file", file);
  return fetch(`/api/portfolios/${portfolioId}/transactions/import`, {
    method: "POST",
    body: form,
  }).then(async (res) => {
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return (await res.json()) as ImportResult;
  });
}

export interface DataUpdateStatus {
  running: boolean;
  last_run: {
    status?: string;
    last_run_at?: string;
    duration_s?: number;
    steps?: Record<string, unknown>;
    warnings?: string[];
    freshness?: Record<string, { last_ts: string | null; symbols: number; age_days: number | null }>;
    errors?: string[];
  } | null;
}

export function fetchUpdateStatus(): Promise<DataUpdateStatus> {
  return get<DataUpdateStatus>("/api/data-update/status");
}

export function triggerUpdate(): Promise<{ status: string }> {
  return postJson<{ status: string }>("/api/data-update/run", {});
}
