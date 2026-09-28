import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  fetchPortfolios,
  runRebalance,
  saveRebalanceRules,
  type RebalanceRules,
  type RebalanceResult,
  type RebalanceTarget,
} from "../api/client";

function fmt(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function Rebalancing() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [targets, setTargets] = useState<RebalanceTarget[]>([]);
  const [driftBand, setDriftBand] = useState("5");
  const [years, setYears] = useState("10");
  const [result, setResult] = useState<RebalanceResult | null>(null);
  const [rulesLoadedFor, setRulesLoadedFor] = useState<number | null>(null);

  const { data: portfolios } = useQuery({ queryKey: ["portfolios"], queryFn: fetchPortfolios });

  const run = useMutation({
    mutationFn: (rules: RebalanceRules | null) =>
      runRebalance(selectedId!, { rules, years: Number(years) || 10 }),
    onSuccess: setResult,
  });
  const saveRules = useMutation({
    mutationFn: (rules: RebalanceRules) => saveRebalanceRules(selectedId!, rules),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["portfolios"] }),
  });

  const loadPortfolio = (id: number) => {
    setSelectedId(id);
    setResult(null);
    const p = portfolios?.find((x) => x.id === id);
    if (p) {
      const rules = p.rebalancing_rules as Partial<RebalanceRules> | undefined;
      setTargets(rules?.targets ?? []);
      setDriftBand(String(rules?.drift_band_pct ?? 5));
      setRulesLoadedFor(rules?.targets ? id : null);
    }
  };

  const currentRules = (): RebalanceRules => ({
    targets,
    drift_band_pct: Number(driftBand) || 0,
  });

  const chartData =
    result?.suggestions.map((s) => ({
      symbol: s.symbol,
      current: s.current_pct,
      target: s.target_pct,
    })) ?? [];

  const actions = result?.suggestions.filter((s) => s.action !== "hold") ?? [];

  return (
    <section>
      <h2>Rebalancing</h2>

      <div className="picker-row">
        {portfolios && portfolios.length > 0 ? (
          <>
            <label htmlFor="rebal-picker" className="muted">
              Portfolio
            </label>
            <select id="rebal-picker" value={selectedId ?? ""} onChange={(e) => e.target.value && loadPortfolio(Number(e.target.value))}>
              <option value="">Select…</option>
              {portfolios.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </>
        ) : (
          <span className="muted">No portfolios yet — create one on the Wealth page.</span>
        )}
      </div>

      {selectedId !== null && (
        <>
          <h3>Target allocation</h3>
          {targets.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th className="num">Target %</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {targets.map((t, i) => (
                  <tr key={i}>
                    <td>{t.symbol}</td>
                    <td className="num">
                      <input
                        type="number"
                        step="any"
                        value={String(t.target_pct)}
                        onChange={(e) => {
                          const next = [...targets];
                          next[i] = { ...t, target_pct: Number(e.target.value) };
                          setTargets(next);
                        }}
                        style={{ width: 90 }}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="small-danger"
                        onClick={() => setTargets(targets.filter((_, idx) => idx !== i))}
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="filter-row">
            <button
              type="button"
              onClick={() => setTargets([...targets, { symbol: "", target_pct: 10 }])}
            >
              + Target
            </button>
            <label className="muted" htmlFor="drift-band">
              Drift band ±%
            </label>
            <input
              id="drift-band"
              type="number"
              step="any"
              value={driftBand}
              onChange={(e) => setDriftBand(e.target.value)}
              style={{ width: 80 }}
            />
            <label className="muted" htmlFor="proj-years">
              Projection years
            </label>
            <input
              id="proj-years"
              type="number"
              value={years}
              onChange={(e) => setYears(e.target.value)}
              style={{ width: 80 }}
            />
            <button
              type="button"
              onClick={() => saveRules.mutate(currentRules())}
              disabled={saveRules.isPending}
            >
              Save rules
            </button>
            <button
              type="button"
              onClick={() => run.mutate(currentRules())}
              disabled={run.isPending}
            >
              Run rebalance
            </button>
          </div>
          {saveRules.isSuccess && <p className="muted">Rules saved.</p>}
          {saveRules.isError && <p className="error-text">{saveRules.error.message}</p>}
          {rulesLoadedFor === selectedId && targets.length > 0 && (
            <p className="muted">Loaded saved rules for this portfolio.</p>
          )}
        </>
      )}

      {run.isError && <p className="error-text">{run.error.message}</p>}

      {result && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Portfolio value</div>
              <div className="stat-value">₹{fmt(result.total_value)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Est. tax on sells</div>
              <div className="stat-value">₹{fmt(result.tax_total_est)}</div>
              <div className="muted">FIFO, equity rates</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Projected value ({result.projection.years}y)</div>
              <div className="stat-value">
                {result.projection.projected_value === null ? "—" : `₹${fmt(result.projection.projected_value)}`}
              </div>
              <div className="muted">
                {result.projection.blended_return_pct === null
                  ? "needs fresh price history"
                  : `at ${result.projection.blended_return_pct}% blended CAGR`}
              </div>
            </div>
          </div>

          {result.untaxed_symbols.length > 0 && (
            <p className="muted">Tax not estimated for: {result.untaxed_symbols.join(", ")}</p>
          )}

          <h3>Suggestions (largest drift first)</h3>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Action</th>
                <th className="num">Amount</th>
                <th className="num">Current %</th>
                <th className="num">Target %</th>
                <th className="num">Drift</th>
                <th className="num">Tax est</th>
              </tr>
            </thead>
            <tbody>
              {result.suggestions.map((s) => (
                <tr key={s.symbol}>
                  <td>{s.symbol}</td>
                  <td className={s.action === "buy" ? "up" : s.action === "sell" ? "down" : "muted"}>
                    {s.action}
                  </td>
                  <td className="num">{s.amount > 0 ? `₹${fmt(s.amount)}` : "—"}</td>
                  <td className="num">{s.current_pct.toFixed(1)}%</td>
                  <td className="num">{s.target_pct.toFixed(1)}%</td>
                  <td className="num">
                    {s.drift_pct >= 0 ? "+" : ""}
                    {s.drift_pct.toFixed(1)}%
                  </td>
                  <td className="num">{s.tax_est > 0 ? `₹${fmt(s.tax_est)}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {actions.length === 0 && <p className="muted">All holdings inside the drift band — nothing to do.</p>}

          <h3>Current vs target allocation</h3>
          <ResponsiveContainer width="100%" height={Math.max(200, chartData.length * 44)}>
            <BarChart data={chartData} layout="vertical" margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="#1e293b" />
              <XAxis type="number" tick={{ fontSize: 11, fill: "#64748b" }} unit="%" />
              <YAxis type="category" dataKey="symbol" tick={{ fontSize: 11, fill: "#94a3b8" }} width={100} />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
                formatter={(value) => `${Number(value).toFixed(1)}%`}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="current" fill="#2563eb" isAnimationActive={false} name="Current %" />
              <Bar dataKey="target" fill="#4ade80" isAnimationActive={false} name="Target %" />
            </BarChart>
          </ResponsiveContainer>
        </>
      )}
    </section>
  );
}
