import logging
import sqlite3
from datetime import date
from pathlib import Path

import polars as pl

from app.config import settings

logger = logging.getLogger(__name__)

# yfinance line items vary in coverage; each canonical metric falls back
# through these candidates (verified against NSE statement data).
FLOW_ITEMS = {
    "revenue": ["TotalRevenue", "OperatingRevenue", "Revenues"],
    "net_income": ["NetIncome", "NetIncomeCommonStockholders"],
    "ebit": ["EBIT", "OperatingIncome"],
    "ebitda": ["NormalizedEBITDA", "EBITDA"],
    "free_cash_flow": ["FreeCashFlow"],
}

STOCK_ITEMS = {
    "equity": ["CommonStockEquity", "StockholdersEquity"],
    "total_assets": ["TotalAssets"],
    "current_liabilities": [
        "CurrentLiabilities",
        "TotalLiabilitiesNetMinorityInterest",
    ],
    "total_debt": ["TotalDebt"],
    "shares": ["OrdinarySharesNumber", "ShareIssued"],
}

ALL_ITEMS = {**FLOW_ITEMS, **STOCK_ITEMS}

# Screener canonical line items (source of truth for unified view)
SCREENER_FLOW_ITEMS = {
    "revenue": ["Sales +", "Revenue +", "TotalRevenue", "OperatingRevenue", "Revenues"],
    "net_income": ["Net Profit +", "NetIncome", "NetIncomeCommonStockholders"],
    "ebit": ["Operating Profit", "EBIT", "OperatingIncome"],
    "ebitda": ["Operating Profit", "NormalizedEBITDA", "EBITDA"],
    "free_cash_flow": ["Free Cash Flow", "FreeCashFlow"],
}

SCREENER_STOCK_ITEMS = {
    "equity": ["Equity Capital", "CommonStockEquity", "StockholdersEquity"],
    "total_assets": ["Total Assets", "TotalAssets"],
    "current_liabilities": ["Other Liabilities +", "CurrentLiabilities", "TotalLiabilitiesNetMinorityInterest"],
    "total_debt": ["Borrowings +", "TotalDebt"],
    "shares": ["OrdinarySharesNumber", "ShareIssued"],
}

SCREENER_ALL_ITEMS = {**SCREENER_FLOW_ITEMS, **SCREENER_STOCK_ITEMS}

# yfinance -> screener canonical mapping (for UI display when falling back)
YFINANCE_TO_SCREENER = {
    "TotalRevenue": "Sales +",
    "OperatingRevenue": "Sales +",
    "Revenues": "Sales +",
    "NetIncome": "Net Profit +",
    "NetIncomeCommonStockholders": "Net Profit +",
    "EBIT": "Operating Profit",
    "OperatingIncome": "Operating Profit",
    "NormalizedEBITDA": "Operating Profit",
    "EBITDA": "Operating Profit",
    "FreeCashFlow": "Free Cash Flow",
    "CommonStockEquity": "Equity Capital",
    "StockholdersEquity": "Equity Capital",
    "TotalAssets": "Total Assets",
    "CurrentLiabilities": "Other Liabilities +",
    "TotalLiabilitiesNetMinorityInterest": "Total Liabilities",
    "TotalDebt": "Borrowings +",
    "OrdinarySharesNumber": "OrdinarySharesNumber",
    "ShareIssued": "ShareIssued",
    "OperatingCashFlow": "Cash from Operating Activity +",
    "InvestingCashFlow": "Cash from Investing Activity +",
    "FinancingCashFlow": "Cash from Financing Activity +",
}

_EMPTY_FIN_SCHEMA = {
    "symbol": pl.String,
    "period_type": pl.String,
    "period_end": pl.Date,
    "statement": pl.String,
    "line_item": pl.String,
    "value": pl.Float64,
}

# SQLite returns DATE columns as ISO strings; parse after construction.
_RAW_ROW_SCHEMA = {
    "symbol": pl.String,
    "period_type": pl.String,
    "period_end": pl.String,
    "statement": pl.String,
    "line_item": pl.String,
    "value": pl.Float64,
}

METRIC_COLUMNS = [
    "market_cap",
    "pe",
    "pb",
    "roe",
    "roce",
    "debt_to_equity",
    "net_margin",
    "operating_margin",
    "revenue_growth",
    "earnings_growth",
]


def load_financials(symbols: list[str] | None = None) -> pl.DataFrame:
    """Load raw financial statement rows from SQLite into Polars."""
    path = settings.data_dir / "mehl.db"
    if not path.exists():
        return pl.DataFrame(schema=_EMPTY_FIN_SCHEMA)
    con = sqlite3.connect(path)
    try:
        query = (
            "SELECT symbol, period_type, period_end, statement, line_item, value FROM financials"
        )
        params: list[object] = []
        if symbols:
            query += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
            params = list(symbols)
        rows = con.execute(query, params).fetchall()
    finally:
        con.close()
    if not rows:
        return pl.DataFrame(schema=_EMPTY_FIN_SCHEMA)
    return (
        pl.DataFrame(rows, schema=_RAW_ROW_SCHEMA, orient="row")
        .with_columns(pl.col("period_end").str.to_date("%Y-%m-%d"))
        .select(_EMPTY_FIN_SCHEMA.keys())
    )


def pivot_financials(df: pl.DataFrame) -> pl.DataFrame:
    """One row per (symbol, period_type, period_end) with line items as columns."""
    if df.is_empty():
        return pl.DataFrame()
    piv = df.pivot(
        values="value",
        index=["symbol", "period_type", "period_end"],
        on="line_item",
        aggregate_function="first",
    )
    return _add_canonical_columns(piv)


def _add_canonical_columns(df: pl.DataFrame) -> pl.DataFrame:
    exprs: list[pl.Expr] = []
    for canonical, candidates in ALL_ITEMS.items():
        existing = [pl.col(c) for c in candidates if c in df.columns]
        if existing:
            exprs.append(pl.coalesce(existing).alias(canonical))
        else:
            exprs.append(pl.lit(None, dtype=pl.Float64).alias(canonical))
    return df.with_columns(exprs)


def _safe_div(num: pl.Expr, den: pl.Expr) -> pl.Expr:
    return pl.when(den.is_not_null() & (den != 0)).then(num / den).otherwise(None)


def compute_universe_metrics(
    fin_piv: pl.DataFrame,
    prices: pl.DataFrame | None = None,
    shares_override: pl.DataFrame | None = None,
    max_price_age_days: int = 120,
) -> pl.DataFrame:
    """Vectorized fundamental metrics for every symbol in the pivot frame.

    prices: latest snapshot (columns symbol, close, optionally last_ts).
    shares_override: columns symbol, shares_outstanding (from securities_info).
    Flow metrics use TTM (sum of last 4 quarters) when the quarterly window is
    complete and stable (max within 10x the median absolute value); otherwise
    they fall back to the latest annual. Price-derived ratios are nulled when
    the latest close is older than max_price_age_days.
    """
    if fin_piv.is_empty():
        return pl.DataFrame(
            schema={"symbol": pl.String, **{c: pl.Float64 for c in METRIC_COLUMNS}}
        )
    canonical = list(ALL_ITEMS)
    flow_cols = list(FLOW_ITEMS)

    annual = fin_piv.filter(pl.col("period_type") == "annual").sort("period_end")
    latest = annual.group_by("symbol").agg(
        [pl.col(c).last().alias(c) for c in canonical]
        + [pl.col("period_end").last().alias("period_end")]
    )
    prev = annual.group_by("symbol").agg(
        [pl.col(c).tail(2).first().alias(f"{c}_prev") for c in flow_cols]
    )
    quarterly = fin_piv.filter(pl.col("period_type") == "quarterly").sort("period_end")
    ttm = quarterly.group_by("symbol").agg(
        [pl.col(c).tail(4).sum().alias(f"{c}_ttm") for c in flow_cols]
        + [pl.col(c).tail(4).abs().median().alias(f"{c}_qmed") for c in flow_cols]
        + [pl.col(c).tail(4).abs().max().alias(f"{c}_qmax") for c in flow_cols]
        + [pl.col(c).tail(4).count().alias(f"{c}_qcount") for c in flow_cols]
    )

    out = latest.join(prev, on="symbol", how="left").join(ttm, on="symbol", how="left")
    if shares_override is not None and not shares_override.is_empty():
        out = out.join(shares_override, on="symbol", how="left")
    if prices is not None and not prices.is_empty():
        pcols = ["symbol", "close"] + (["last_ts"] if "last_ts" in prices.columns else [])
        out = out.join(prices.select(pcols), on="symbol", how="left")
        if "last_ts" in out.columns:
            age = (pl.lit(date.today()) - pl.col("last_ts").cast(pl.Date)).dt.total_days()
            out = out.with_columns(
                pl.when(age > max_price_age_days).then(None).otherwise(pl.col("close")).alias("close")
            )

    shares_exprs = [pl.col("shares")]
    if "shares_outstanding" in out.columns:
        shares_exprs.insert(0, pl.col("shares_outstanding"))
    shares = pl.coalesce(shares_exprs)
    close = pl.col("close") if "close" in out.columns else pl.lit(None, dtype=pl.Float64)
    market_cap = pl.when(shares.is_not_null() & (shares > 0)).then(shares * close).otherwise(None)

    def stable_ttm(item: str) -> pl.Expr:
        ttm_col = pl.col(f"{item}_ttm")
        annual_col = pl.col(item)
        complete = pl.col(f"{item}_qcount") >= 4
        med = pl.col(f"{item}_qmed")
        qmax = pl.col(f"{item}_qmax")
        stable = complete & med.is_not_null() & (med > 0) & (qmax <= 10 * med)
        return pl.when(stable).then(ttm_col).otherwise(annual_col)

    revenue = stable_ttm("revenue")
    net_income = stable_ttm("net_income")
    ebit = stable_ttm("ebit")
    equity = pl.col("equity")
    ce = (pl.col("total_assets") - pl.col("current_liabilities")).alias("_ce")

    out = out.with_columns(ce).with_columns(
        market_cap.alias("market_cap"),
        pl.when(net_income.is_not_null() & (net_income > 0))
        .then(_safe_div(market_cap, net_income))
        .otherwise(None)
        .alias("pe"),
        _safe_div(market_cap, equity).alias("pb"),
        pl.when(equity.is_not_null() & (equity > 0))
        .then(net_income / equity)
        .otherwise(None)
        .alias("roe"),
        pl.when(pl.col("_ce").is_not_null() & (pl.col("_ce") > 0))
        .then(_safe_div(ebit, pl.col("_ce")))
        .otherwise(None)
        .alias("roce"),
        _safe_div(pl.col("total_debt"), equity).alias("debt_to_equity"),
        _safe_div(net_income, revenue).alias("net_margin"),
        _safe_div(ebit, revenue).alias("operating_margin"),
        _safe_div(pl.col("revenue"), pl.col("revenue_prev")).alias("revenue_growth_raw"),
        _safe_div(pl.col("net_income"), pl.col("net_income_prev")).alias(
            "earnings_growth_raw"
        ),
    )
    out = out.with_columns(
        (pl.col("revenue_growth_raw") - 1.0).alias("revenue_growth"),
        (pl.col("earnings_growth_raw") - 1.0).alias("earnings_growth"),
    )
    keep = [
        "symbol",
        "period_end",
        "market_cap",
        "pe",
        "pb",
        "roe",
        "roce",
        "debt_to_equity",
        "net_margin",
        "operating_margin",
        "revenue_growth",
        "earnings_growth",
        "revenue",
        "net_income",
        "ebitda",
        "free_cash_flow",
    ]
    if "close" in out.columns:
        keep.append("close")
    if "shares_outstanding" in out.columns:
        keep.append("shares_outstanding")
    return out.select(keep)


def compute_symbol_history(
    fin_piv: pl.DataFrame, prices: pl.DataFrame | None = None
) -> list[dict]:
    """Per-period fundamental metrics for one symbol, for trend charts.

    pe/pb use the close at (or before) each period end; flow ratios are
    annualized for quarterly periods.
    """
    if fin_piv.is_empty():
        return []
    df = _add_canonical_columns(fin_piv).sort("period_end")
    if prices is not None and not prices.is_empty():
        p = prices.with_columns(pl.col("ts").cast(pl.Date).alias("_pdate")).sort("_pdate")
        df = df.join_asof(
            p.select("_pdate", "close"),
            left_on="period_end",
            right_on="_pdate",
            strategy="backward",
        )
    else:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))

    annualize = pl.when(pl.col("period_type") == "quarterly").then(4.0).otherwise(1.0)
    shares = pl.col("shares")
    equity = pl.col("equity")
    ce = pl.col("total_assets") - pl.col("current_liabilities")

    df = df.with_columns(
        pl.when(shares.is_not_null() & (shares > 0) & (pl.col("net_income").is_not_null()))
        .then(pl.col("close") * shares / pl.col("net_income"))
        .otherwise(None)
        .alias("pe"),
        pl.when(shares.is_not_null() & (shares > 0) & equity.is_not_null() & (equity > 0))
        .then(pl.col("close") * shares / equity)
        .otherwise(None)
        .alias("pb"),
        pl.when(equity.is_not_null() & (equity > 0) & pl.col("net_income").is_not_null())
        .then(annualize * pl.col("net_income") / equity)
        .otherwise(None)
        .alias("roe"),
        pl.when(ce.is_not_null() & (ce > 0) & pl.col("ebit").is_not_null())
        .then(annualize * pl.col("ebit") / ce)
        .otherwise(None)
        .alias("roce"),
        _safe_div(pl.col("total_debt"), equity).alias("debt_to_equity"),
        _safe_div(pl.col("net_income"), pl.col("revenue")).alias("net_margin"),
        _safe_div(pl.col("ebit"), pl.col("revenue")).alias("operating_margin"),
    )
    out = df.select(
        "period_end",
        "period_type",
        "pe",
        "pb",
        "roe",
        "roce",
        "debt_to_equity",
        "net_margin",
        "operating_margin",
    )
    return out.to_dicts()


def _load_screener_financials(symbols: list[str] | None = None) -> pl.DataFrame:
    """Load screener financials from financials_screener table."""
    path = settings.data_dir / "mehl.db"
    if not path.exists():
        return pl.DataFrame(schema=_EMPTY_FIN_SCHEMA)
    con = sqlite3.connect(path)
    try:
        query = (
            "SELECT symbol, period_type, period_end, statement, line_item, value, "
            "consolidation, source FROM financials_screener"
        )
        params: list[object] = []
        if symbols:
            query += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
            params = list(symbols)
        rows = con.execute(query, params).fetchall()
    finally:
        con.close()
    if not rows:
        return pl.DataFrame(schema={
            **_EMPTY_FIN_SCHEMA,
            "consolidation": pl.String,
            "source": pl.String,
        })
    return (
        pl.DataFrame(rows, schema={
            "symbol": pl.String,
            "period_type": pl.String,
            "period_end": pl.String,
            "statement": pl.String,
            "line_item": pl.String,
            "value": pl.Float64,
            "consolidation": pl.String,
            "source": pl.String,
        }, orient="row")
        .with_columns(pl.col("period_end").str.to_date("%Y-%m-%d"))
    )


def _load_yfinance_financials(symbols: list[str] | None = None) -> pl.DataFrame:
    """Load yfinance financials with screener canonical line items."""
    df = load_financials(symbols)
    if df.is_empty():
        return pl.DataFrame(schema={
            **_EMPTY_FIN_SCHEMA,
            "consolidation": pl.String,
            "source": pl.String,
        })
    # Map yfinance line items to screener canonical names
    df = df.with_columns(
        pl.col("line_item").replace(YFINANCE_TO_SCREENER, default=pl.col("line_item")).alias("line_item"),
        pl.lit("yfinance").alias("source"),
        pl.lit("consolidated").alias("consolidation"),
    )
    # Ensure same column order as screener
    return df.select([
        "symbol", "period_type", "period_end", "statement", 
        "line_item", "value", "consolidation", "source"
    ])


def load_financials_unified(
    symbols: list[str] | None = None,
    consolidation: str = "consolidated",
) -> pl.DataFrame:
    """Load financials with priority: screener consolidated -> screener standalone -> yfinance.
    
    Returns unified frame with columns:
    symbol, period_type, period_end, statement, line_item, value, source, consolidation
    """
    # 1. Load screener data (both consolidated and standalone)
    screener_df = _load_screener_financials(symbols)
    
    if not screener_df.is_empty():
        # Filter by consolidation preference
        if consolidation == "consolidated":
            # Prioritize consolidated, fill gaps with standalone
            consolidated_df = screener_df.filter(pl.col("consolidation") == "consolidated")
            standalone_df = screener_df.filter(pl.col("consolidation") == "standalone")
            
            # Find missing keys in consolidated that exist in standalone
            if not consolidated_df.is_empty() and not standalone_df.is_empty():
                keys = ["symbol", "period_type", "period_end", "statement", "line_item"]
                consolidated_keys = consolidated_df.select(keys).unique()
                missing = standalone_df.join(consolidated_keys, on=keys, how="anti")
                screener_df = pl.concat([consolidated_df, missing])
            else:
                screener_df = consolidated_df
        elif consolidation == "standalone":
            screener_df = screener_df.filter(pl.col("consolidation") == "standalone")
    else:
        screener_df = pl.DataFrame(schema={
            **_EMPTY_FIN_SCHEMA,
            "consolidation": pl.String,
            "source": pl.String,
        })
    
    # 2. Load yfinance as fallback
    yfinance_df = _load_yfinance_financials(symbols)
    
    # 3. Merge: screener first, then yfinance for missing keys
    if not screener_df.is_empty() and not yfinance_df.is_empty():
        keys = ["symbol", "period_type", "period_end", "statement", "line_item"]
        screener_keys = screener_df.select(keys).unique()
        yfinance_missing = yfinance_df.join(screener_keys, on=keys, how="anti")
        unified = pl.concat([screener_df, yfinance_missing])
    elif not screener_df.is_empty():
        unified = screener_df
    elif not yfinance_df.is_empty():
        unified = yfinance_df
    else:
        unified = pl.DataFrame(schema={
            **_EMPTY_FIN_SCHEMA,
            "consolidation": pl.String,
            "source": pl.String,
        })
    
    # Normalize statement names: yfinance uses 'income'/'balance'/'cashflow', screener uses 'profit_loss'/'balance_sheet'/'cash_flows'
    stmt_map = {
        "income": "profit_loss",
        "balance": "balance_sheet",
        "cashflow": "cash_flows",
    }
    unified = unified.with_columns(
        pl.col("statement").replace(stmt_map, default=pl.col("statement")).alias("statement")
    )
    
    return unified.select(list(_EMPTY_FIN_SCHEMA.keys()) + ["source", "consolidation"])


def pivot_financials_unified(df: pl.DataFrame) -> pl.DataFrame:
    """Pivot unified financials using screener canonical line items."""
    if df.is_empty():
        return pl.DataFrame()
    
    # Use screener canonical items for pivot
    all_items = {**SCREENER_FLOW_ITEMS, **SCREENER_STOCK_ITEMS}
    
    piv = df.pivot(
        values="value",
        index=["symbol", "period_type", "period_end"],
        on="line_item",
        aggregate_function="first",
    )
    
    # Add canonical columns using screener items
    exprs: list[pl.Expr] = []
    for canonical, candidates in SCREENER_ALL_ITEMS.items():
        existing = [pl.col(c) for c in candidates if c in piv.columns]
        if existing:
            exprs.append(pl.coalesce(existing).alias(canonical))
        else:
            exprs.append(pl.lit(None, dtype=pl.Float64).alias(canonical))
    
    # Also add yfinance-specific items that might be in the data
    for canonical, candidates in ALL_ITEMS.items():
        if canonical not in SCREENER_ALL_ITEMS:
            existing = [pl.col(c) for c in candidates if c in piv.columns]
            if existing:
                exprs.append(pl.coalesce(existing).alias(canonical))
            else:
                exprs.append(pl.lit(None, dtype=pl.Float64).alias(canonical))
    
    piv = piv.with_columns(exprs)
    
    # Deduplicate annual rows by fiscal year (year of period_end)
    # Keep the row with the most non-null canonical values
    annual_mask = pl.col("period_type") == "annual"
    non_annual = piv.filter(~annual_mask)
    annual = piv.filter(annual_mask)
    
    if not annual.is_empty():
        # Add fiscal year column
        annual = annual.with_columns(
            pl.col("period_end").dt.year().alias("fiscal_year")
        )
        # Count non-null canonical columns per row
        canonical_cols = [c for c in SCREENER_ALL_ITEMS.keys() if c in annual.columns]
        if canonical_cols:
            annual = annual.with_columns(
                pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Int32) for c in canonical_cols]).alias("_non_null_count")
            )
            # Keep row with max non-null count per fiscal_year per symbol
            annual = annual.sort(["symbol", "fiscal_year", "_non_null_count"], descending=[False, False, True])
            annual = annual.unique(subset=["symbol", "fiscal_year"], keep="first")
            annual = annual.drop(["fiscal_year", "_non_null_count"])
    
    return pl.concat([annual, non_annual]).sort(["symbol", "period_type", "period_end"])
