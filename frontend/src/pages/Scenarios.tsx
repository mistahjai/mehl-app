import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  deleteResource,
  fetchScenarios,
  goalPlan,
  previewScenario,
  saveScenario,
  updateScenario,
  type ScenarioParams,
  type ScenarioRunResult,
  type SavedScenario,
  defaultScenarioParams,
} from "../api/client";

function fmt(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

const NUM_FIELDS: { key: keyof ScenarioParams; label: string; pct?: boolean }[] = [
  { key: "start_value", label: "Start value (₹)" },
  { key: "monthly_contribution", label: "Monthly SIP (₹)" },
  { key: "annual_step_up_pct", label: "Annual step-up", pct: true },
  { key: "expected_return", label: "Expected return", pct: true },
  { key: "volatility", label: "Volatility", pct: true },
  { key: "years", label: "Years" },
  { key: "inflation", label: "Inflation", pct: true },
];

export function Scenarios() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [params, setParams] = useState<ScenarioParams>(defaultScenarioParams());
  const [result, setResult] = useState<ScenarioRunResult | null>(null);
  const [goal, setGoal] = useState<{ target: string; years: string; ret: string }>({
    target: "",
    years: "20",
    ret: "12",
  });

  const { data: scenarios } = useQuery({ queryKey: ["scenarios"], queryFn: fetchScenarios });

  const run = useMutation({
    mutationFn: () => previewScenario(params, 2000),
    onSuccess: setResult,
  });
  const save = useMutation({
    mutationFn: () => saveScenario(name.trim(), params),
    onSuccess: () => {
      setName("");
      queryClient.invalidateQueries({ queryKey: ["scenarios"] });
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => deleteResource(`/api/scenarios/${id}`),
    onSuccess: () => {
      setName("");
      setParams(defaultScenarioParams());
      setResult(null);
      queryClient.invalidateQueries({ queryKey: ["scenarios"] });
    },
  });
  const loadScenario = (s: SavedScenario) => {
    setName(s.name);
    setParams({ ...defaultScenarioParams(), ...s.params });
  };
  const saveUpdate = useMutation({
    mutationFn: (s: SavedScenario) => updateScenario(s.id, s.name, params),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scenarios"] }),
  });
  const goalMutation = useMutation({
    mutationFn: () =>
      goalPlan({
        target: Number(goal.target),
        years: Number(goal.years),
        expected_return: Number(goal.ret) / 100,
      }),
  });

  const setNum = (key: keyof ScenarioParams, value: string) => {
    setParams({ ...params, [key]: Number(value) });
  };

  const chartData =
    result === null
      ? []
      : result.projection.map((row, i) => {
          const band = result.monte_carlo.bands[i] ?? {};
          return {
            month: row.month,
            date: row.date,
            p10: band.p10 ?? null,
            p50: band.p50 ?? null,
            p90: band.p90 ?? null,
            det: row.value,
            invested: row.invested,
          };
        });

  const finalPct = result?.monte_carlo.final_percentiles ?? null;
  const lastProjection = result ? result.projection[result.projection.length - 1] : null;

  return (
    <section>
      <h2>Scenario Builder</h2>

      <div className="filter-row">
        <label className="muted" htmlFor="saved-scenario">
          Saved scenario
        </label>
        <select
          id="saved-scenario"
          value=""
          onChange={(e) => {
            const s = scenarios?.find((x) => x.id === Number(e.target.value));
            if (s) loadScenario(s);
          }}
        >
          <option value="">Load…</option>
          {scenarios?.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        {scenarios && scenarios.length > 0 && (
          <span className="muted">
            {scenarios.length} saved · latest: {scenarios[0].name}
          </span>
        )}
      </div>

      <div className="scenario-form">
        {NUM_FIELDS.map((f) => (
          <label key={f.key} className="field-label">
            {f.label}
            <input
              type="number"
              step="any"
              value={String(params[f.key])}
              onChange={(e) => setNum(f.key, e.target.value)}
            />
          </label>
        ))}
      </div>

      <h3>Asset allocation (optional — blends return & volatility)</h3>
      {params.assets.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th className="num">Weight</th>
              <th className="num">Return</th>
              <th className="num">Vol</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {params.assets.map((a, i) => (
              <tr key={i}>
                <td>
                  <input
                    value={a.name}
                    onChange={(e) => {
                      const assets = [...params.assets];
                      assets[i] = { ...a, name: e.target.value };
                      setParams({ ...params, assets });
                    }}
                  />
                </td>
                {(["weight", "expected_return", "volatility"] as const).map((k) => (
                  <td key={k} className="num">
                    <input
                      type="number"
                      step="any"
                      value={String(a[k])}
                      onChange={(e) => {
                        const assets = [...params.assets];
                        assets[i] = { ...a, [k]: Number(e.target.value) };
                        setParams({ ...params, assets });
                      }}
                      style={{ width: 90 }}
                    />
                  </td>
                ))}
                <td>
                  <button
                    type="button"
                    className="small-danger"
                    onClick={() =>
                      setParams({ ...params, assets: params.assets.filter((_, idx) => idx !== i) })
                    }
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
          onClick={() =>
            setParams({
              ...params,
              assets: [
                ...params.assets,
                { name: `asset-${params.assets.length + 1}`, weight: 1, expected_return: 0.12, volatility: 0.15 },
              ],
            })
          }
        >
          + Asset
        </button>
        <button type="button" onClick={() => run.mutate()} disabled={run.isPending}>
          Run scenario
        </button>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Scenario name" />
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={name.trim() === "" || save.isPending}
        >
          Save
        </button>
      </div>
      {run.isError && <p className="error-text">{run.error.message}</p>}
      {save.isError && <p className="error-text">{save.error.message}</p>}

      {result && lastProjection && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Projected value (p50)</div>
              <div className="stat-value">₹{fmt(finalPct?.p50)}</div>
              <div className="muted">invested ₹{fmt(lastProjection.invested)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Downside (p10)</div>
              <div className="stat-value">₹{fmt(finalPct?.p10)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Upside (p90)</div>
              <div className="stat-value">₹{fmt(finalPct?.p90)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Real value (inflation-adj)</div>
              <div className="stat-value">₹{fmt(lastProjection.real_value)}</div>
              <div className="muted">today's purchasing power</div>
            </div>
          </div>

          <h3>Projection with Monte Carlo bands</h3>
          <ResponsiveContainer width="100%" height={380}>
            <AreaChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="#1e293b" />
              <XAxis dataKey="month" tick={{ fontSize: 11, fill: "#64748b" }} />
              <YAxis tick={{ fontSize: 11, fill: "#64748b" }} tickFormatter={(v) => `${(v / 100000).toFixed(0)}L`} />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
                formatter={(value) => `₹${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Area dataKey="p10" stackId="band" stroke="none" fill="transparent" name="p10" />
              <Area
                dataKey="p90"
                stackId="band"
                stroke="none"
                fill="rgba(37, 99, 235, 0.18)"
                name="p10-p90 band"
                connectNulls
              />
              <Line dataKey="p50" stroke="#4ade80" dot={false} isAnimationActive={false} name="p50 (MC)" />
              <Line dataKey="det" stroke="#2563eb" dot={false} isAnimationActive={false} name="Deterministic" />
              <Line
                dataKey="invested"
                stroke="#f59e0b"
                strokeDasharray="4 4"
                dot={false}
                isAnimationActive={false}
                name="Invested"
              />
            </AreaChart>
          </ResponsiveContainer>
          {scenarios && scenarios.some((s) => s.name === name.trim()) && (
            <div className="filter-row">
              <span className="muted">'{name.trim()}' is saved — update it with current params:</span>
              <button
                type="button"
                onClick={() => {
                  const s = scenarios.find((x) => x.name === name.trim());
                  if (s) saveUpdate.mutate(s);
                }}
                disabled={saveUpdate.isPending}
              >
                Update saved
              </button>
              <button
                type="button"
                className="small-danger"
                onClick={() => {
                  const s = scenarios.find((x) => x.name === name.trim());
                  if (s) remove.mutate(s.id);
                }}
                disabled={remove.isPending}
              >
                Delete saved
              </button>
            </div>
          )}
        </>
      )}

      <h3>Goal planner</h3>
      <div className="filter-row">
        <input
          type="number"
          value={goal.target}
          onChange={(e) => setGoal({ ...goal, target: e.target.value })}
          placeholder="Target corpus (₹)"
          aria-label="Target corpus"
        />
        <input
          type="number"
          value={goal.years}
          onChange={(e) => setGoal({ ...goal, years: e.target.value })}
          placeholder="Years"
          aria-label="Years"
        />
        <input
          type="number"
          value={goal.ret}
          onChange={(e) => setGoal({ ...goal, ret: e.target.value })}
          placeholder="Return %"
          aria-label="Expected return percent"
        />
        <button
          type="button"
          onClick={() => goalMutation.mutate()}
          disabled={!goal.target || goalMutation.isPending}
        >
          Required SIP
        </button>
      </div>
      {goalMutation.data && (
        <p>
          You need <strong>₹{fmt(goalMutation.data.required_monthly_contribution)}/month</strong> for{" "}
          {goalMutation.data.years} years to reach ₹{fmt(goalMutation.data.target)}.
        </p>
      )}
      {goalMutation.isError && <p className="error-text">{goalMutation.error.message}</p>}
    </section>
  );
}
