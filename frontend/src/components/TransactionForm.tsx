import { useEffect, useState } from "react";
import { fetchCloseOn } from "../api/client";
import { SymbolSearch } from "./SymbolSearch";

export interface NewTransaction {
  trade_date: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  fees: number;
  note?: string | null;
}

export function TransactionForm({
  onSubmit,
  pending,
}: {
  onSubmit: (txn: NewTransaction) => void;
  pending: boolean;
}) {
  const [tradeDate, setTradeDate] = useState(new Date().toISOString().slice(0, 10));
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [fees, setFees] = useState("0");

  useEffect(() => {
    if (!symbol || Number(price) > 0) return;
    let cancelled = false;
    fetchCloseOn(symbol, tradeDate)
      .then((r) => {
        if (!cancelled && r.close !== null) setPrice(String(r.close));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [symbol, tradeDate, price]);

  const valid =
    symbol.trim() !== "" &&
    Number(quantity) > 0 &&
    Number(price) >= 0 &&
    Number(fees) >= 0;

  return (
    <form
      className="txn-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!valid) return;
        onSubmit({
          trade_date: tradeDate,
          symbol: symbol.trim().toUpperCase(),
          side,
          quantity: Number(quantity),
          price: Number(price),
          fees: Number(fees),
        });
        setSymbol("");
        setQuantity("");
        setPrice("");
        setFees("0");
      }}
    >
      <input type="date" value={tradeDate} onChange={(e) => setTradeDate(e.target.value)} aria-label="Date" />
      <SymbolSearch
        onSelect={(t) => setSymbol(t.symbol)}
        placeholder={symbol ? "Change symbol…" : "Search symbol…"}
      />
      {symbol && (
        <span className="symbol-chip type-stock">
          {symbol}
          <button
            type="button"
            onClick={() => {
              setSymbol("");
              setPrice("");
            }}
            aria-label="Clear symbol"
          >
            ✕
          </button>
        </span>
      )}
      <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")} aria-label="Side">
        <option value="buy">Buy</option>
        <option value="sell">Sell</option>
      </select>
      <input
        type="number"
        value={quantity}
        onChange={(e) => setQuantity(e.target.value)}
        placeholder="Qty"
        aria-label="Quantity"
        step="any"
      />
      <input
        type="number"
        value={price}
        onChange={(e) => setPrice(e.target.value)}
        placeholder="Price"
        aria-label="Price"
        step="any"
        title="Leave 0 to auto-fill from the closing price on the trade date"
      />
      <input
        type="number"
        value={fees}
        onChange={(e) => setFees(e.target.value)}
        placeholder="Fees"
        aria-label="Fees"
        step="any"
      />
      <button type="submit" disabled={!valid || pending}>
        Add
      </button>
    </form>
  );
}

export interface NewDividend {
  ex_date: string;
  symbol: string;
  amount_per_share: number | null;
  total_amount: number | null;
  note?: string | null;
}

export function DividendForm({
  onSubmit,
  pending,
}: {
  onSubmit: (dividend: NewDividend) => void;
  pending: boolean;
}) {
  const [exDate, setExDate] = useState(new Date().toISOString().slice(0, 10));
  const [symbol, setSymbol] = useState("");
  const [amountPerShare, setAmountPerShare] = useState("");
  const [totalAmount, setTotalAmount] = useState("");

  const valid =
    symbol.trim() !== "" && (Number(amountPerShare) > 0 || Number(totalAmount) > 0);

  return (
    <form
      className="txn-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!valid) return;
        onSubmit({
          ex_date: exDate,
          symbol: symbol.trim().toUpperCase(),
          amount_per_share: amountPerShare !== "" ? Number(amountPerShare) : null,
          total_amount: totalAmount !== "" ? Number(totalAmount) : null,
        });
        setSymbol("");
        setAmountPerShare("");
        setTotalAmount("");
      }}
    >
      <input type="date" value={exDate} onChange={(e) => setExDate(e.target.value)} aria-label="Ex-date" />
      <SymbolSearch onSelect={(t) => setSymbol(t.symbol)} placeholder={symbol ? "Change symbol…" : "Search symbol…"} />
      {symbol && (
        <span className="symbol-chip type-stock">
          {symbol}
          <button
            type="button"
            onClick={() => setSymbol("")}
            aria-label="Clear symbol"
          >
            ✕
          </button>
        </span>
      )}
      <input
        type="number"
        value={amountPerShare}
        onChange={(e) => setAmountPerShare(e.target.value)}
        placeholder="Per share"
        aria-label="Amount per share"
        step="any"
      />
      <input
        type="number"
        value={totalAmount}
        onChange={(e) => setTotalAmount(e.target.value)}
        placeholder="Total"
        aria-label="Total amount"
        step="any"
      />
      <button type="submit" disabled={!valid || pending}>
        Add
      </button>
    </form>
  );
}
