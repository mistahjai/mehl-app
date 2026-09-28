import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  runScreener,
  fetchSavedScreens,
  type ScreenFilter,
  type ScreenerResponse,
} from "../api/client";

const METRICS = [
  "pe",
  "pb",
  "roe",
  "roce",
  "debt_to_equity",
  "net_margin",
  "operating_margin",
  "revenue_growth",
  "earnings_growth",
  "market_cap",
  "close",
  "volume",
];

const OPS = ["lt", "lte", "gt", "gte", "eq", "between"] as const;

const LABELS: Record<string, string> = {
  pe: "P/E",
  pb: "P/B",
  roe: "ROE",
  roce: "ROCE",
  debt_to_equity: "D/E",
  net_margin: "Net margin",
  operating_margin: "Op margin",
  revenue_growth: "Rev growth",
  earnings_growth: "Earn growth",
  market_cap: "Market cap",
  close: "Price",
  volume: "Volume",
};

function fmt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function Markets() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<ScreenFilter[]>([
    { metric: "pe", op: "lt", value: 30 },
  ]);
  const [rankBy, setRankBy] = useState<string>("pe");
  const [rankDesc, setRankDesc] = useState(false);

  const { data: savedScreens } = useQuery({
    queryKey: ["screens"],
    queryFn: fetchSavedScreens,
  });

  const mutation = useMutation<ScreenerResponse, Error, ScreenFilter[]>({
    mutationFn: (f) =>
      runScreener({
        filters: f,
        rank_by: rankBy || null,
        rank_desc: rankDesc,
        limit: 100,
      }),
  });

  const updateFilter = (i: number, patch: Partial<ScreenFilter>) => {
    setFilters(filters.map((f, idx) => (idx === i ? { ...f, ...patch } : f)));
  };

  return (
    <section>
      <h2>Markets · Screener</h2>
      <div className="filter-builder">
        {filters.map((f, i) => (
          <div key={i} className="filter-row">
            <select
              value={f.metric}
              onChange={(e) => updateFilter(i, { metric: e.target.value })}
              aria-label="Metric"
            >
              {METRICS.map((m) => (
                <option key={m} value={m}>
                  {LABELS[m] ?? m}
                </option>
              ))}
            </select>
            <select
              value={f.op}
              onChange={(e) => updateFilter(i, { op: e.target.value as ScreenFilter["op"] })}
              aria-label="Operator"
            >
              {OPS.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
            {f.op === "between" ? (
              <>
                <input
                  type="number"
                  value={Array.isArray(f.value) ? f.value[0] : 0}
                  onChange={(e) =>
                    updateFilter(i, {
                      value: [Number(e.target.value), Array.isArray(f.value) ? f.value[1] : 0],
                    })
                  }
                  aria-label="Minimum"
                />
                <input
                  type="number"
                  value={Array.isArray(f.value) ? f.value[1] : 0}
                  onChange={(e) =>
                    updateFilter(i, {
                      value: [Array.isArray(f.value) ? f.value[0] : 0, Number(e.target.value)],
                    })
                  }
                  aria-label="Maximum"
                />
              </>
            ) : (
              <input
                type="number"
                value={typeof f.value === "number" ? f.value : 0}
                onChange={(e) => updateFilter(i, { value: Number(e.target.value) })}
                aria-label="Value"
              />
            )}
            <button type="button" onClick={() => setFilters(filters.filter((_, idx) => idx !== i))}>
              ✕
            </button>
          </div>
        ))}
        <div className="filter-row">
          <button type="button" onClick={() => setFilters([...filters, { metric: "pe", op: "lt", value: 20 }])}>
            + Filter
          </button>
          <label className="muted" htmlFor="rank-select">
            Rank by
          </label>
          <select id="rank-select" value={rankBy} onChange={(e) => setRankBy(e.target.value)}>
            <option value="">None</option>
            {METRICS.map((m) => (
              <option key={m} value={m}>
                {LABELS[m] ?? m}
              </option>
            ))}
          </select>
          <label className="muted">
            <input
              type="checkbox"
              checked={rankDesc}
              onChange={(e) => setRankDesc(e.target.checked)}
            />{" "}
            descending
          </label>
          <button type="button" onClick={() => mutation.mutate(filters)} disabled={mutation.isPending}>
            Run screen
          </button>
        </div>
      </div>

      {mutation.isError && <p className="error-text">{mutation.error.message}</p>}
      {mutation.data && (
        <>
          <p className="muted">
            {mutation.data.total} matches, showing {mutation.data.rows.length}
          </p>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Name</th>
                <th>Sector</th>
                <th className="num">Price</th>
                <th className="num">P/E</th>
                <th className="num">P/B</th>
                <th className="num">ROE</th>
                <th className="num">D/E</th>
                <th className="num">Net margin</th>
                <th className="num">Rev growth</th>
              </tr>
            </thead>
            <tbody>
              {mutation.data.rows.map((row) => (
                <tr
                  key={row.symbol}
                  className="clickable"
                  onClick={() => navigate(`/markets/${encodeURIComponent(row.symbol)}`)}
                >
                  <td>{row.symbol}</td>
                  <td className="muted">{row.name ?? ""}</td>
                  <td className="muted">{row.sector ?? ""}</td>
                  <td className="num">{fmt(row.close)}</td>
                  <td className="num">{fmt(row.pe, 1)}</td>
                  <td className="num">{fmt(row.pb, 1)}</td>
                  <td className="num">{row.roe === null ? "—" : `${(row.roe * 100).toFixed(1)}%`}</td>
                  <td className="num">{fmt(row.debt_to_equity, 2)}</td>
                  <td className="num">
                    {row.net_margin === null ? "—" : `${(row.net_margin * 100).toFixed(1)}%`}
                  </td>
                  <td className="num">
                    {row.revenue_growth === null ? "—" : `${(row.revenue_growth * 100).toFixed(1)}%`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {savedScreens && savedScreens.length > 0 && (
        <>
          <h3>Saved screens</h3>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Filters</th>
                <th>Ranked by</th>
              </tr>
            </thead>
            <tbody>
              {savedScreens.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td className="muted">
                    {s.filters
                      .map((f) => `${LABELS[f.metric] ?? f.metric} ${f.op} ${f.value}`)
                      .join(", ")}
                  </td>
                  <td className="muted">{s.rank_by ? (LABELS[s.rank_by] ?? s.rank_by) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
