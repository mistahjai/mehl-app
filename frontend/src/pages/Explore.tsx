import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  fetchIndices,
  fetchOhlcv,
  fetchOverview,
  type IndexRow,
  type OhlcvRow,
  type Ticker,
} from "../api/client";
import { CloseChart } from "../components/CloseChart";
import { SymbolSearch } from "../components/SymbolSearch";

function fmt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
}

const RETURN_LABELS: Record<string, string> = {
  ytd: "YTD",
  "1y": "1Y",
  "3y": "3Y",
  "5y": "5Y",
  "10y": "10Y",
};

const METRIC_LABELS: Record<string, string> = {
  market_cap: "Market cap",
  pe: "P/E",
  pb: "P/B",
  roe: "ROE",
  roce: "ROCE",
  debt_to_equity: "D/E",
  net_margin: "Net margin",
  operating_margin: "Op margin",
  revenue: "Revenue (TTM)",
  net_income: "Profit (TTM)",
  revenue_growth: "Rev growth",
  earnings_growth: "Earn growth",
};

export function Explore() {
  const [selected, setSelected] = useState<Ticker | null>(null);
  const symbol = selected?.symbol ?? "";

  const { data: overview } = useQuery({
    queryKey: ["overview", symbol],
    queryFn: () => fetchOverview(symbol),
    enabled: symbol !== "",
  });

  const { data: points, isError: historyError } = useQuery({
    queryKey: ["explore-history", symbol],
    queryFn: async (): Promise<{ ts: string; close: number }[]> => {
      if (overview?.type === "index") {
        const idx: IndexRow[] = await fetchIndices(symbol);
        return idx.map((r) => ({ ts: r.ts, close: r.close }));
      }
      const ohlcv: OhlcvRow[] = await fetchOhlcv(symbol, 100_000);
      return ohlcv.map((r) => ({ ts: r.ts, close: r.close }));
    },
    enabled: symbol !== "" && overview !== undefined,
  });

  const priceLabel =
    overview?.type === "mf" ? "NAV" : overview?.type === "index" ? "Level" : "Price";

  return (
    <section>
      <h2>Explore</h2>
      <p className="muted">
        Search any stock, ETF, mutual fund or index by symbol or name, then inspect its
        full price history and key numbers.
      </p>

      <div className="explore-search">
        <SymbolSearch
          autoFocus
          onSelect={setSelected}
          placeholder={selected ? "Search another ticker…" : "Search stock / ETF / MF / index…"}
        />
        {selected && (
          <span className={`symbol-chip type-${selected.type}`}>
            {selected.symbol}
            <button type="button" onClick={() => setSelected(null)} aria-label="Clear selection">
              ✕
            </button>
          </span>
        )}
      </div>

      {selected && overview && (
        <>
          <h3 className="explore-title">
            {selected.symbol}
            <span className="muted">
              {" "}
              · {overview.name ?? selected.name ?? ""}
              {selected.date_first_entry && (
                <>
                  {" "}
                  · data {selected.date_first_entry} → {selected.date_last_entry}
                </>
              )}
            </span>
          </h3>

          {historyError && (
            <p className="error-text">Failed to load price history for {selected.symbol}.</p>
          )}
          {points && <CloseChart points={points} />}

          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">
                {priceLabel} · as of {overview.last_ts?.slice(0, 10) ?? "—"}
              </div>
              <div className="stat-value">
                {overview.type === "index"
                  ? fmt(overview.price, 2)
                  : `₹${fmt(overview.price)}`}
              </div>
            </div>
            {Object.entries(RETURN_LABELS).map(([key, label]) => (
              <div key={key} className="stat-card">
                <div className="stat-label">{label} return</div>
                <div className={`stat-value ${(overview.returns[key] ?? 0) >= 0 ? "up" : "down"}`}>
                  {pct(overview.returns[key])}
                </div>
              </div>
            ))}
          </div>

          {Object.keys(overview.metrics).length > 0 && (
            <>
              <h3>Key metrics</h3>
              <div className="stat-grid">
                {Object.entries(overview.metrics).map(([key, value]) => (
                  <div key={key} className="stat-card">
                    <div className="stat-label">{METRIC_LABELS[key] ?? key}</div>
                    <div className="stat-value">
                      {value === null
                        ? "—"
                        : key === "roe" ||
                            key === "net_margin" ||
                            key === "operating_margin" ||
                            key === "revenue_growth" ||
                            key === "earnings_growth"
                          ? `${(Number(value) * 100).toFixed(1)}%`
                          : fmt(Number(value), 1)}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {overview.type === "mf" && (
            <p className="muted">
              Mutual fund NAVs are published once per business day; ratios like P/E don't
              apply — trailing returns are the primary lens.
            </p>
          )}
        </>
      )}

      {!selected && (
        <p className="muted">Pick a ticker above to see its chart and metrics.</p>
      )}
    </section>
  );
}
