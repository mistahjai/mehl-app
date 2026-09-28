import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  backtestAllocation,
  fetchIndexSummaries,
  type AllocationBacktestResult,
  type BacktestMetrics,
} from "../api/client";

function fmt(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function MetricsCards({ metrics, title }: { metrics: BacktestMetrics; title: string }) {
  const cards: [string, string, string | null][] = [
    ["XIRR", pct(metrics.xirr_pct), "money-weighted annualized"],
    ["Final value", `₹${fmt(metrics.final_value)}`, `invested ₹${fmt(metrics.total_invested)}`],
    ["Max drawdown", pct(metrics.max_drawdown_pct), "on portfolio value"],
    ["Volatility", pct(metrics.volatility_pct), "annualized monthly"],
    ["Sharpe", metrics.sharpe === null ? "—" : metrics.sharpe.toFixed(2), "vs 7% risk-free"],
    ["Total return", pct(metrics.total_return_pct), "final / invested"],
  ];
  return (
    <div>
      <h3>{title}</h3>
      <div className="stat-grid">
        {cards.map(([label, value, sub]) => (
          <div key={label} className="stat-card">
            <div className="stat-label">{label}</div>
            <div className="stat-value">{value}</div>
            {sub && <div className="muted">{sub}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

export function Backtest() {
  const [symbols, setSymbols] = useState("RELIANCE,TCS");
  const [weights, setWeights] = useState("50,50");
  const [start, setStart] = useState("");
  const [initialCapital, setInitialCapital] = useState("100000");
  const [monthlySip, setMonthlySip] = useState("10000");
  const [stepUp, setStepUp] = useState("0");
  const [rebalanceFreq, setRebalanceFreq] = useState("");
  const [benchmark, setBenchmark] = useState("NIFTY_50");

  const { data: indexList } = useQuery({
    queryKey: ["index-summaries"],
    queryFn: fetchIndexSummaries,
  });

  const mutation = useMutation<AllocationBacktestResult, Error, void>({
    mutationFn: () => {
      const symbolList = symbols
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const weightList = weights
        .split(",")
        .map((w) => Number(w.trim()))
        .filter((w) => !Number.isNaN(w) && w > 0);
      return backtestAllocation({
        symbols: symbolList,
        weights: weightList.length === symbolList.length ? weightList : null,
        start: start || null,
        initial_capital: Number(initialCapital) || 0,
        monthly_contribution: Number(monthlySip) || 0,
        annual_step_up_pct: (Number(stepUp) || 0) / 100,
        rebalance_freq: rebalanceFreq || null,
        benchmark: benchmark || "NIFTY_50",
      });
    },
  });

  const chartData = mutation.data
    ? mutation.data.strategy.curve.map((row, i) => ({
        date: row.date,
        strategy: row.value,
        invested: row.invested,
        benchmark: mutation.data?.benchmark?.curve[i]?.value ?? null,
      }))
    : [];

  return (
    <section>
      <h2>Backtesting</h2>
      <p className="muted">
        Monthly allocation backtests with SIP contributions and rebalancing intervals,
        benchmarked against an index. Prices are corporate-action adjusted.
      </p>

      <div className="scenario-form">
        <label className="field-label">
          Symbols (comma separated)
          <input value={symbols} onChange={(e) => setSymbols(e.target.value)} />
        </label>
        <label className="field-label">
          Weights (matching order)
          <input value={weights} onChange={(e) => setWeights(e.target.value)} />
        </label>
        <label className="field-label">
          Start date
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="field-label">
          Initial capital (₹)
          <input type="number" value={initialCapital} onChange={(e) => setInitialCapital(e.target.value)} />
        </label>
        <label className="field-label">
          Monthly SIP (₹)
          <input type="number" value={monthlySip} onChange={(e) => setMonthlySip(e.target.value)} />
        </label>
        <label className="field-label">
          Annual step-up %
          <input type="number" step="any" value={stepUp} onChange={(e) => setStepUp(e.target.value)} />
        </label>
        <label className="field-label">
          Rebalance
          <select value={rebalanceFreq} onChange={(e) => setRebalanceFreq(e.target.value)}>
            <option value="">Buy & hold</option>
            <option value="monthly">Monthly</option>
            <option value="quarterly">Quarterly</option>
            <option value="yearly">Yearly</option>
          </select>
        </label>
        <label className="field-label">
          Benchmark
          <select value={benchmark} onChange={(e) => setBenchmark(e.target.value)}>
            <option value="">None</option>
            {(indexList ?? [])
              .filter((i) => i.is_usable)
              .map((i) => (
                <option key={i.symbol} value={i.symbol}>
                  {i.symbol.replace(/_/g, " ")}
                </option>
              ))}
          </select>
        </label>
      </div>
      <div className="filter-row">
        <button type="button" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
          Run backtest
        </button>
      </div>
      {mutation.isError && <p className="error-text">{mutation.error.message}</p>}

      {mutation.data && (
        <>
          {mutation.data.missing_symbols.length > 0 && (
            <p className="muted">
              No price data for: {mutation.data.missing_symbols.join(", ")} — run the data backfills.
            </p>
          )}
          <MetricsCards metrics={mutation.data.strategy.metrics} title="Strategy" />
          {mutation.data.benchmark && (
            <MetricsCards
              metrics={mutation.data.benchmark.metrics}
              title={`Benchmark · ${mutation.data.benchmark.symbol}`}
            />
          )}

          <h3>Equity curve vs benchmark</h3>
          <ResponsiveContainer width="100%" height={380}>
            <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="#1e293b" />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#64748b" }} />
              <YAxis
                tick={{ fontSize: 11, fill: "#64748b" }}
                tickFormatter={(v) => `${(v / 100000).toFixed(0)}L`}
              />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
                formatter={(value) => `₹${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line dataKey="strategy" stroke="#2563eb" dot={false} isAnimationActive={false} name="Strategy" />
              {mutation.data.benchmark && (
                <Line
                  dataKey="benchmark"
                  stroke="#f59e0b"
                  dot={false}
                  isAnimationActive={false}
                  name={mutation.data.benchmark.symbol}
                />
              )}
              <Line
                dataKey="invested"
                stroke="#64748b"
                strokeDasharray="4 4"
                dot={false}
                isAnimationActive={false}
                name="Invested"
              />
            </LineChart>
          </ResponsiveContainer>

          {mutation.data.strategy.metrics.rolling_returns &&
            Object.keys(mutation.data.strategy.metrics.rolling_returns).length > 0 && (
              <>
                <h3>Rolling returns (strategy)</h3>
                <table>
                  <thead>
                    <tr>
                      <th>Window</th>
                      <th className="num">Worst</th>
                      <th className="num">Best</th>
                      <th className="num">Latest</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(mutation.data.strategy.metrics.rolling_returns).map(
                      ([window, r]) => (
                        <tr key={window}>
                          <td>{window}</td>
                          <td className="num">{pct(r.min_pct)}</td>
                          <td className="num">{pct(r.max_pct)}</td>
                          <td className="num">{pct(r.latest_pct)}</td>
                        </tr>
                      ),
                    )}
                  </tbody>
                </table>
              </>
            )}
        </>
      )}
    </section>
  );
}
