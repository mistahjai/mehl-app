import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { HoldingSummary } from "../api/client";

const COLORS = ["#2563eb", "#4ade80", "#f59e0b", "#8b5cf6", "#f87171", "#06b6d4", "#ec4899", "#84cc16"];

export function AllocationDonut({ holdings }: { holdings: HoldingSummary[] }) {
  const data = holdings
    .filter((h) => h.current_value !== null && h.current_value > 0)
    .map((h) => ({ name: h.symbol, value: h.current_value as number }));

  if (data.length === 0) {
    return <p className="muted">No valued holdings yet.</p>;
  }

  return (
    <ResponsiveContainer width="100%" height={240}>
      <PieChart>
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          innerRadius="55%"
          outerRadius="85%"
          paddingAngle={2}
          isAnimationActive={false}
        >
          {data.map((_, i) => (
            <Cell key={i} fill={COLORS[i % COLORS.length]} />
          ))}
        </Pie>
        <Tooltip
          contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
          formatter={(value) => Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
