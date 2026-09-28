import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  fetchIndexSummaries,
  fetchIndices,
  fetchUpdateStatus,
  fetchWatchlist,
  triggerUpdate,
} from "../api/client";

export function Dashboard() {
  const [indexSymbol, setIndexSymbol] = useState("NIFTY_50");
  const { data: watchlist } = useQuery({
    queryKey: ["watchlist"],
    queryFn: fetchWatchlist,
  });
  const { data: indexList } = useQuery({
    queryKey: ["index-summaries"],
    queryFn: fetchIndexSummaries,
  });
  const { data: series } = useQuery({
    queryKey: ["index", indexSymbol],
    queryFn: () => fetchIndices(indexSymbol),
  });
  const { data: updateStatus, refetch: refetchStatus } = useQuery({
    queryKey: ["data-update-status"],
    queryFn: fetchUpdateStatus,
    refetchInterval: (query) => (query.state.data?.running ? 3000 : false),
  });

  const trigger = useMutation({
    mutationFn: triggerUpdate,
    onSuccess: () => refetchStatus(),
  });

  const running = updateStatus?.running ?? false;
  const lastRun = updateStatus?.last_run ?? null;

  const latest = series && series.length > 0 ? series[series.length - 1] : null;
  const first = series && series.length > 0 ? series[0] : null;
  const changePct =
    latest && first && first.close > 0
      ? ((latest.close / first.close - 1) * 100).toFixed(2)
      : null;
  const usable = (indexList ?? []).filter((i) => i.is_usable);
  const rest = (indexList ?? []).filter((i) => !i.is_usable);
  const selectedSummary = (indexList ?? []).find((i) => i.symbol === indexSymbol);

  return (
    <section>
      <div className="picker-row">
        <h2>Dashboard</h2>
        <button
          type="button"
          disabled={running || trigger.isPending}
          onClick={() => trigger.mutate()}
        >
          {running ? "Updating…" : "Update data"}
        </button>
        {lastRun && (
          <span className="muted">
            last run {lastRun.last_run_at?.slice(0, 16).replace("T", " ") ?? "—"} ·{" "}
            {lastRun.status ?? "—"}
            {lastRun.errors && lastRun.errors.length > 0 && (
              <> · {lastRun.errors.length} error(s): {lastRun.errors.join("; ")}</>
            )}
          </span>
        )}
        {!lastRun && !running && (
          <span className="muted">daily auto-update at 17:30 UTC</span>
        )}
      </div>
      {lastRun?.freshness && (
        <div className="picker-row">
          {Object.entries(lastRun.freshness).map(([name, info]) => (
            <span key={name} className={info.age_days !== null && info.age_days > 4 ? "down" : "muted"}>
              {name.replace(/_/g, " ")}: {info.last_ts ?? "no data"} ({info.symbols})
            </span>
          ))}
        </div>
      )}
      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">
            <select
              value={indexSymbol}
              onChange={(e) => setIndexSymbol(e.target.value)}
              aria-label="Index"
            >
              <optgroup label="Updated daily">
                {usable.map((i) => (
                  <option key={i.symbol} value={i.symbol}>
                    {i.symbol.replace(/_/g, " ")}
                  </option>
                ))}
              </optgroup>
              {rest.length > 0 && (
                <optgroup label="Limited or retired — history only">
                  {rest.map((i) => (
                    <option key={i.symbol} value={i.symbol}>
                      {i.symbol.replace(/_/g, " ")}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>{" "}
            (loaded range)
          </div>
          <div className="stat-value">
            {latest ? latest.close.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}
          </div>
          {changePct !== null && (
            <div className={Number(changePct) >= 0 ? "up" : "down"}>
              {Number(changePct) >= 0 ? "+" : ""}
              {changePct}%
            </div>
          )}
          {latest && <div className="muted">as of {latest.ts.slice(0, 10)}</div>}
          {selectedSummary && !selectedSummary.is_usable && (
            <div className="muted">
              {selectedSummary.rows < 30
                ? "no usable history on this source yet"
                : "no longer updated by NSE"}
            </div>
          )}
        </div>
        <div className="stat-card">
          <div className="stat-label">Watchlist</div>
          <div className="stat-value">{watchlist ? watchlist.length : "—"}</div>
          <div className="muted">tracked instruments</div>
        </div>
      </div>
      {watchlist && watchlist.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Note</th>
            </tr>
          </thead>
          <tbody>
            {watchlist.map((item) => (
              <tr key={item.id}>
                <td>{item.symbol}</td>
                <td className="muted">{item.note ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
