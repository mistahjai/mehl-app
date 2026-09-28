import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { fetchFundamentals, fetchStatements, type StatementsResponse } from "../api/client";
import { PriceChart } from "../components/PriceChart";
import { RatioCharts } from "../components/RatioChart";

function fmt(v: number | string | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function StatementTable({ rows }: { rows: StatementsResponse["statements"][string] }) {
  const periods: string[] = [];
  const items = new Map<string, Map<string, number | null>>();
  for (const r of rows) {
    const period = r.period_end.slice(0, 10);
    if (!periods.includes(period)) periods.push(period);
    if (!items.has(r.line_item)) items.set(r.line_item, new Map());
    items.get(r.line_item)!.set(period, r.value);
  }
  const top = [...items.entries()]
    .map(([item, byPeriod]) => ({
      item,
      nonNull: [...byPeriod.values()].filter((v) => v !== null).length,
      byPeriod,
    }))
    .sort((a, b) => b.nonNull - a.nonNull)
    .slice(0, 20);

  return (
    <table>
      <thead>
        <tr>
          <th>Line item</th>
          {periods.map((p) => (
            <th key={p} className="num">
              {p}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {top.map(({ item, byPeriod }) => (
          <tr key={item}>
            <td>{item.replace(/([a-z])([A-Z])/g, "$1 $2")}</td>
            {periods.map((p) => (
              <td key={p} className="num">
                {fmt(byPeriod.get(p) ?? null, 1)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function SecurityDetail() {
  const { symbol = "" } = useParams<{ symbol: string }>();
  const { data: fundamentals } = useQuery({
    queryKey: ["fundamentals", symbol],
    queryFn: () => fetchFundamentals(symbol),
    enabled: symbol !== "",
  });
  const { data: statements } = useQuery({
    queryKey: ["statements", symbol],
    queryFn: () => fetchStatements(symbol),
    enabled: symbol !== "",
  });

  const latest = fundamentals?.latest ?? null;

  return (
    <section>
      <h2>
        {symbol}
        {latest?.sector !== null && latest?.sector !== undefined && (
          <span className="muted">
            {" "}
            · {String(latest.sector)}
            {latest.industry ? ` / ${String(latest.industry)}` : ""}
          </span>
        )}
      </h2>

      <PriceChart symbol={symbol} />

      <h3>Fundamentals</h3>
      <div className="stat-grid">
        {[
          ["Market cap", fmt(latest?.market_cap, 0)],
          ["P/E", fmt(latest?.pe, 1)],
          ["P/B", fmt(latest?.pb, 1)],
          ["ROE", latest?.roe == null ? "—" : `${(Number(latest.roe) * 100).toFixed(1)}%`],
          ["ROCE", latest?.roce == null ? "—" : `${(Number(latest.roce) * 100).toFixed(1)}%`],
          ["D/E", fmt(latest?.debt_to_equity, 2)],
        ].map(([label, value]) => (
          <div key={label} className="stat-card">
            <div className="stat-label">{label}</div>
            <div className="stat-value">{value}</div>
          </div>
        ))}
      </div>

      {fundamentals && fundamentals.history.length > 0 && (
        <>
          <h3>Ratio trends</h3>
          <RatioCharts history={fundamentals.history} />
        </>
      )}

      {statements && Object.keys(statements.statements).length > 0 && (
        <>
          {Object.entries(statements.statements).map(([name, rows]) => (
            <div key={name}>
              <h3 className="capitalize">{name} statement</h3>
              <StatementTable rows={rows} />
            </div>
          ))}
        </>
      )}
    </section>
  );
}
