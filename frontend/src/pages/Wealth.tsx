import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  addDividend,
  addTransaction,
  createPortfolio,
  deleteResource,
  fetchEquityCurve,
  fetchGains,
  fetchSummary,
  fetchTransactions,
  importTransactionsCsv,
  type DividendInput,
  type ImportResult,
} from "../api/client";
import type { NewTransaction } from "../components/TransactionForm";
import { AllocationDonut } from "../components/AllocationDonut";
import { DividendForm, TransactionForm } from "../components/TransactionForm";
import { EquityCurveChart } from "../components/EquityCurveChart";

function fmt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
}

export function Wealth() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [newName, setNewName] = useState("");
  const [importResult, setImportResult] = useState<ImportResult | null>(null);

  const { data: portfolios } = useQuery({ queryKey: ["portfolios"], queryFn: async () => {
    const res = await fetch("/api/portfolios");
    if (!res.ok) throw new Error(`${res.status}`);
    return (await res.json()) as { id: number; name: string }[];
  } });

  const { data: summary } = useQuery({
    queryKey: ["summary", selectedId],
    queryFn: () => fetchSummary(selectedId!),
    enabled: selectedId !== null,
  });
  const { data: transactions } = useQuery({
    queryKey: ["transactions", selectedId],
    queryFn: () => fetchTransactions(selectedId!),
    enabled: selectedId !== null,
  });
  const { data: dividends } = useQuery({
    queryKey: ["dividends", selectedId],
    queryFn: async () => {
      const res = await fetch(`/api/portfolios/${selectedId}/dividends`);
      if (!res.ok) throw new Error(`${res.status}`);
      return (await res.json()) as { id: number; ex_date: string; symbol: string; amount_per_share: number | null; total_amount: number | null }[];
    },
    enabled: selectedId !== null,
  });
  const { data: gains } = useQuery({
    queryKey: ["gains", selectedId],
    queryFn: () => fetchGains(selectedId!),
    enabled: selectedId !== null,
  });
  const { data: curve } = useQuery({
    queryKey: ["equity-curve", selectedId],
    queryFn: () => fetchEquityCurve(selectedId!),
    enabled: selectedId !== null,
  });

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: ["summary", selectedId] });
    queryClient.invalidateQueries({ queryKey: ["transactions", selectedId] });
    queryClient.invalidateQueries({ queryKey: ["dividends", selectedId] });
    queryClient.invalidateQueries({ queryKey: ["gains", selectedId] });
    queryClient.invalidateQueries({ queryKey: ["equity-curve", selectedId] });
  };

  const addTxn = useMutation({
    mutationFn: (txn: NewTransaction) => addTransaction(selectedId!, txn),
    onSuccess: invalidateAll,
  });
  const addDiv = useMutation({
    mutationFn: (d: DividendInput) => addDividend(selectedId!, d),
    onSuccess: invalidateAll,
  });
  const createPortfolioMutation = useMutation({
    mutationFn: () => createPortfolio(newName.trim()),
    onSuccess: (portfolio) => {
      setNewName("");
      queryClient.invalidateQueries({ queryKey: ["portfolios"] });
      setSelectedId(portfolio.id);
    },
  });
  const deleteTxn = useMutation({
    mutationFn: (id: number) => deleteResource(`/api/portfolios/${selectedId}/transactions/${id}`),
    onSuccess: invalidateAll,
  });
  const deleteDiv = useMutation({
    mutationFn: (id: number) => deleteResource(`/api/portfolios/${selectedId}/dividends/${id}`),
    onSuccess: invalidateAll,
  });
  const importCsv = useMutation({
    mutationFn: (file: File) => importTransactionsCsv(selectedId!, file),
    onSuccess: (result: ImportResult) => {
      setImportResult(result);
      invalidateAll();
    },
  });

  const selected = portfolios?.find((p) => p.id === selectedId) ?? null;

  return (
    <section>
      <h2>Wealth Manager</h2>

      <div className="picker-row">
        {portfolios && portfolios.length > 0 ? (
          <>
            <label htmlFor="portfolio-picker" className="muted">
              Portfolio
            </label>
            <select
              id="portfolio-picker"
              value={selectedId ?? ""}
              onChange={(e) => setSelectedId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Select…</option>
              {portfolios.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </>
        ) : (
          <span className="muted">No portfolios yet — create one:</span>
        )}
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="New portfolio name"
          aria-label="New portfolio name"
        />
        <button
          type="button"
          disabled={newName.trim() === "" || createPortfolioMutation.isPending}
          onClick={() => createPortfolioMutation.mutate()}
        >
          Create
        </button>
      </div>

      {selected && summary && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Current value</div>
              <div className="stat-value">₹{fmt(summary.current_value, 0)}</div>
              <div className={summary.unrealized >= 0 ? "up" : "down"}>{fmt(summary.unrealized, 0)} unrealized</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Invested</div>
              <div className="stat-value">₹{fmt(summary.invested, 0)}</div>
              <div className="muted">cost basis of open holdings</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">XIRR (incl. dividends)</div>
              <div className="stat-value">{summary.xirr === null ? "—" : pct(summary.xirr)}</div>
              <div className="muted">money-weighted annualized</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Realized P&L</div>
              <div className="stat-value">₹{fmt(summary.realized, 0)}</div>
              <div className={summary.realized >= 0 ? "up" : "down"}>avg-cost method</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Dividends received</div>
              <div className="stat-value">₹{fmt(summary.dividends, 0)}</div>
              <div className="muted">all time</div>
            </div>
          </div>

          {curve && curve.length > 0 && (
            <>
              <h3>Portfolio value</h3>
              <EquityCurveChart points={curve} />
            </>
          )}

          <div className="two-col">
            <div>
              <h3>Holdings</h3>
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="num">Qty</th>
                    <th className="num">Avg cost</th>
                    <th className="num">Price</th>
                    <th className="num">Value</th>
                    <th className="num">P&L</th>
                    <th className="num">Realized</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.holdings.map((h) => (
                    <tr key={h.symbol}>
                      <td>{h.symbol}</td>
                      <td className="num">{fmt(h.quantity, 0)}</td>
                      <td className="num">{fmt(h.avg_cost)}</td>
                      <td className="num">{fmt(h.close)}</td>
                      <td className="num">{fmt(h.current_value, 0)}</td>
                      <td className={`num ${(h.unrealized ?? 0) >= 0 ? "up" : "down"}`}>
                        {h.unrealized === null ? "—" : `${fmt(h.unrealized, 0)} (${pct(h.unrealized_pct)})`}
                      </td>
                      <td className="num">{fmt(h.realized, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {summary.holdings.length === 0 && <p className="muted">No open holdings.</p>}
            </div>
            <div>
              <h3>Allocation</h3>
              <AllocationDonut holdings={summary.holdings} />
            </div>
          </div>

          {gains && (
            <>
              <h3>Capital gains (FIFO, equity rates)</h3>
              <div className="stat-grid">
                <div className="stat-card">
                  <div className="stat-label">LTCG (long term)</div>
                  <div className="stat-value">₹{fmt(gains.ltcg, 0)}</div>
                  <div className="muted">
                    est. tax ₹{fmt(gains.ltcg_tax_est, 0)} ({(gains.ltcg_rate * 100).toFixed(1)}% above ₹
                    {fmt(gains.ltcg_exemption, 0)})
                  </div>
                </div>
                <div className="stat-card">
                  <div className="stat-label">STCG (short term)</div>
                  <div className="stat-value">₹{fmt(gains.stcg, 0)}</div>
                  <div className="muted">
                    est. tax ₹{fmt(gains.stcg_tax_est, 0)} ({(gains.stcg_rate * 100).toFixed(1)}%)
                  </div>
                </div>
              </div>
            </>
          )}

          <h3>Transactions</h3>
          <TransactionForm
            onSubmit={(txn) => addTxn.mutate(txn)}
            pending={addTxn.isPending}
          />
          <div className="filter-row">
            <label className="import-label" htmlFor="txn-csv-input">
              Import CSV
            </label>
            <input
              id="txn-csv-input"
              type="file"
              accept=".csv,text/csv"
              disabled={importCsv.isPending}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) importCsv.mutate(file);
                e.target.value = "";
              }}
            />
            <span className="muted">
              columns: date, symbol, side, quantity, price (0 → auto-filled), fees, note
            </span>
          </div>
          {importCsv.isError && <p className="error-text">{importCsv.error.message}</p>}
          {importResult && (
            <p className="muted">
              Imported {importResult.imported} of {importResult.total} rows
              {importResult.skipped.length > 0 && (
                <>
                  {" "}
                  · skipped:{" "}
                  {importResult.skipped
                    .map((s) => `row ${s.row} (${s.reason})`)
                    .join(", ")}
                </>
              )}
            </p>
          )}
          {transactions && transactions.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Qty</th>
                  <th className="num">Price</th>
                  <th className="num">Fees</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {transactions.map((t) => (
                  <tr key={t.id}>
                    <td>{t.trade_date}</td>
                    <td>{t.symbol}</td>
                    <td className={t.side === "buy" ? "up" : "down"}>{t.side}</td>
                    <td className="num">{fmt(t.quantity, 0)}</td>
                    <td className="num">{fmt(t.price)}</td>
                    <td className="num">{fmt(t.fees)}</td>
                    <td>
                      <button type="button" className="small-danger" onClick={() => deleteTxn.mutate(t.id)}>
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h3>Dividends</h3>
          <DividendForm onSubmit={(d) => addDiv.mutate(d)} pending={addDiv.isPending} />
          {addDiv.isError && <p className="error-text">{addDiv.error.message}</p>}
          {dividends && dividends.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Ex-date</th>
                  <th>Symbol</th>
                  <th className="num">Per share</th>
                  <th className="num">Total</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {dividends.map((d) => (
                  <tr key={d.id}>
                    <td>{d.ex_date}</td>
                    <td>{d.symbol}</td>
                    <td className="num">{fmt(d.amount_per_share)}</td>
                    <td className="num">{fmt(d.total_amount)}</td>
                    <td>
                      <button type="button" className="small-danger" onClick={() => deleteDiv.mutate(d.id)}>
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
