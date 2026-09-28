import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { FundamentalsResponse } from "../api/client";

type MetricKey = "pe" | "pb" | "roe" | "roce" | "debt_to_equity" | "net_margin";

const CHARTS: { key: MetricKey; label: string; percent?: boolean }[] = [
  { key: "pe", label: "P/E" },
  { key: "pb", label: "P/B" },
  { key: "roe", label: "ROE", percent: true },
  { key: "roce", label: "ROCE", percent: true },
  { key: "debt_to_equity", label: "Debt / Equity" },
  { key: "net_margin", label: "Net margin", percent: true },
];

export function RatioChart({
  metric,
  label,
  percent,
  history,
}: {
  metric: MetricKey;
  label: string;
  percent?: boolean;
  history: FundamentalsResponse["history"];
}) {
  const data = history
    .filter((h) => h[metric] !== null)
    .map((h) => ({ period: h.period_end.slice(0, 10), value: (h[metric] as number) * (percent ? 100 : 1) }));

  if (data.length === 0) {
    return (
      <div className="chart-card">
        <div className="stat-label">{label}</div>
        <p className="muted">No data</p>
      </div>
    );
  }

  return (
    <div className="chart-card">
      <div className="stat-label">{label}</div>
      <ResponsiveContainer width="100%" height={160}>
        <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <XAxis dataKey="period" tick={{ fontSize: 10, fill: "#64748b" }} tickLine={false} />
          <YAxis tick={{ fontSize: 10, fill: "#64748b" }} tickLine={false} width={44} />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
            labelStyle={{ color: "#94a3b8" }}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke="#2563eb"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function RatioCharts({ history }: { history: FundamentalsResponse["history"] }) {
  return (
    <div className="chart-grid">
      {CHARTS.map((c) => (
        <RatioChart key={c.key} metric={c.key} label={c.label} percent={c.percent} history={history} />
      ))}
    </div>
  );
}
