import { useEffect, useRef, useState } from "react";
import { searchTickers, type Ticker } from "../api/client";

export function SymbolSearch({
  onSelect,
  placeholder = "Search symbol or name…",
  types,
  autoFocus = false,
}: {
  onSelect: (ticker: Ticker) => void;
  placeholder?: string;
  types?: string;
  autoFocus?: boolean;
}) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Ticker[]>([]);
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [excludeSme, setExcludeSme] = useState(true);
  const [excludeDelisted, setExcludeDelisted] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (q.trim() === "") {
      setResults([]);
      setOpen(false);
      setError(null);
      return;
    }
    const timer = setTimeout(() => {
      searchTickers(q.trim(), types ?? null, 12, excludeSme, excludeDelisted)
        .then((rows) => {
          setResults(rows);
          setOpen(true);
          setHighlight(0);
          setError(rows.length === 0 ? "no-results" : null);
        })
        .catch((e: unknown) => {
          setResults([]);
          setOpen(true);
          setError(e instanceof Error ? e.message : String(e));
        });
    }, 150);
    return () => clearTimeout(timer);
  }, [q, types, excludeSme, excludeDelisted]);

  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, []);

  const pick = (ticker: Ticker) => {
    onSelect(ticker);
    setQ("");
    setResults([]);
    setOpen(false);
    setError(null);
  };

  const categoryLabel: Record<string, string> = {
    equity: "Equity",
    sme: "SME",
    etf: "ETF",
    mf: "MF",
    "mf-equity": "MF-Equity",
    "mf-debt": "MF-Debt",
    "mf-hybrid": "MF-Hybrid",
    "mf-index": "MF-Index",
    reit: "REIT",
    invit: "InvIT",
    delisted: "Delisted",
    index: "Index",
    other: "Other",
  };

  return (
    <div className="symbol-search" ref={boxRef}>
      <input
        value={q}
        autoFocus={autoFocus}
        placeholder={placeholder}
        aria-label="Search ticker"
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => {
          if (results.length > 0) setOpen(true);
        }}
        onKeyDown={(e) => {
          if (!open || results.length === 0) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setHighlight((h) => Math.min(h + 1, results.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlight((h) => Math.max(h - 1, 0));
          } else if (e.key === "Enter") {
            e.preventDefault();
            pick(results[highlight]);
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
      />
      <div className="symbol-search-filters">
        <label className="filter-checkbox">
          <input
            type="checkbox"
            checked={excludeSme}
            onChange={(e) => setExcludeSme(e.target.checked)}
          />
          <span>Exclude SME</span>
        </label>
        <label className="filter-checkbox">
          <input
            type="checkbox"
            checked={excludeDelisted}
            onChange={(e) => setExcludeDelisted(e.target.checked)}
          />
          <span>Exclude Delisted</span>
        </label>
      </div>
      {open && results.length > 0 && (
        <ul className="symbol-search-results" role="listbox">
          {results.map((t, i) => (
            <li
              key={`${t.type}-${t.symbol}`}
              className={i === highlight ? "active" : ""}
              role="option"
              aria-selected={i === highlight}
              onMouseEnter={() => setHighlight(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(t);
              }}
            >
              <span className="tt-symbol">{t.symbol}</span>
              <span className="tt-name">{t.name ?? ""}</span>
              <span className={`tt-type type-${t.type}`}>{t.type}</span>
              <span className={`tt-category cat-${t.category}`}>{categoryLabel[t.category] ?? t.category}</span>
            </li>
          ))}
        </ul>
      )}
      {open && results.length === 0 && (
        <ul className="symbol-search-results">
          <li className="search-empty" onMouseDown={(e) => e.preventDefault()}>
            {error === "no-results"
              ? `No matches for “${q.trim()}” — the data may not be loaded in this backend`
              : `Search failed: ${error}`}
          </li>
        </ul>
      )}
    </div>
  );
}
