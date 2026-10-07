from datetime import date
from pathlib import Path
import json
import sqlite3

import polars as pl

from app.config import settings
from app.engine import db as market_db

ETF_SYMBOL_PATTERNS = (
    "ETF", "GOLD", "SILVER", "NIFTY", "SENSEX", "LIQUID", "BANK", "MIDCAP",
    "SMALL", "LARGE", "VALUE", "GROWTH", "QUALITY", "MOMENTUM", "ALPHA",
    "EQUAL", "DIVIDEND", "CPSE", "BHARAT", "PSU", "ADD", "NEXT", "NEXT50",
    "MOM50", "QLITY", "LOWVOL", "PVTBANK", "PSUBANK", "MIDBANK", "BOND",
    "INDEX", "FUND", "FOF", "OVERNIGHT", "MONEY", "LIQUID", "ULTRA",
    "SHORT", "DURATION", "CORPORATE", "GOVERNMENT", "STATE", "GSEC",
    "SDL", "TREASURY", "BILL", "FLOATER", "FIXED", "MATURITY", "YEAR",
    "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND"
)

ETF_NAME_PATTERNS = (
    "ETF", "GOLD", "SILVER", "NIFTY", "SENSEX", "LIQUID", "BANK", "MIDCAP",
    "SMALLCAP", "LARGECAP", "VALUE", "GROWTH", "QUALITY", "MOMENTUM", "ALPHA",
    "EQUAL", "DIVIDEND", "CPSE", "BHARAT", "PSU", "BOND", "INDEX FUND",
    "FUND OF FUND", "OVERNIGHT", "MONEY MARKET", "ULTRA SHORT", "SHORT DURATION",
    "CORPORATE BOND", "GOVERNMENT BOND", "STATE DEVELOPMENT", "GSEC", "SDL",
    "TREASURY BILL", "FLOATER", "FIXED MATURITY", "YEAR", "MONTH", "WEEK",
    "HOUR", "MINUTE", "SECOND", "BHARAT BOND", "CPSE BOND"
)

SUSPENDED_SYMBOLS = {
    "IL&FSTRANS", "LAKPRE", "GOLDENTOBC", "CENTEXT-RE", "DISAQ",
    "ELCIDIN", "GUJRAFFIA", "SINGERIND", "TCC", "ZODIAC"
}

RIGHTS_SUFFIXES = ("-RE", "-RE1", "-RE2", "-RE3", "-R", "-R1", "-R2", "-RI")


def is_operating_company(symbol: str, name: str | None, series: str) -> tuple[bool, str | None]:
    if not name or not name.strip():
        return False, "empty_name"

    if symbol.endswith(RIGHTS_SUFFIXES):
        return False, "rights_issue"

    symbol_upper = symbol.upper()
    if any(p in symbol_upper for p in ETF_SYMBOL_PATTERNS):
        return False, "etf_symbol"

    if "DVR" in symbol_upper:
        return False, "dvr"

    name_upper = name.upper()
    if any(p.upper() in name_upper for p in ETF_NAME_PATTERNS):
        return False, "etf_name"

    if symbol in SUSPENDED_SYMBOLS:
        return False, "suspended_delisted"

    return True, None


def get_mainboard_symbols() -> pl.DataFrame:
    path = settings.data_dir / "mehl.db"
    con = sqlite3.connect(path)
    try:
        query = """
            SELECT symbol, name, series, instrument_type, listed_on, last_seen
            FROM securities
            WHERE series IN ('EQ', 'BE', 'BZ') AND instrument_type = 'stock'
            ORDER BY symbol
        """
        df = pl.read_database(query, con)
    finally:
        con.close()

    results = []
    for row in df.iter_rows(named=True):
        is_op, reason = is_operating_company(row["symbol"], row["name"], row["series"])
        results.append({
            "symbol": row["symbol"],
            "name": row["name"],
            "series": row["series"],
            "instrument_type": row["instrument_type"],
            "listed_on": row["listed_on"],
            "last_seen": row["last_seen"],
            "is_operating_company": is_op,
            "filter_reason": reason
        })

    return pl.DataFrame(results)


def get_financial_coverage(symbols: list[str]) -> pl.DataFrame:
    if not symbols:
        return pl.DataFrame(schema={
            "symbol": pl.String,
            "period_type": pl.String,
            "statement": pl.String,
            "source": pl.String,
            "consolidation": pl.String,
            "period_count": pl.Int64,
            "min_period_end": pl.Date,
            "max_period_end": pl.Date,
            "years_covered": pl.Float64
        })

    path = settings.data_dir / "mehl.db"
    con = sqlite3.connect(path)
    try:
        placeholders = ",".join("?" * len(symbols))
        
        # Query yfinance financials
        query_yf = f"""
            SELECT 
                symbol,
                period_type,
                statement,
                'yfinance' as source,
                'consolidated' as consolidation,
                COUNT(DISTINCT period_end) as period_count,
                MIN(period_end) as min_period_end,
                MAX(period_end) as max_period_end
            FROM financials
            WHERE symbol IN ({placeholders})
            GROUP BY symbol, period_type, statement
        """
        cursor = con.cursor()
        cursor.execute(query_yf, symbols)
        rows_yf = cursor.fetchall()
        
        # Query screener financials with normalized statement names
        query_scr = f"""
            SELECT 
                symbol,
                period_type,
                CASE 
                    WHEN statement = 'profit_loss' THEN 'income'
                    WHEN statement = 'balance_sheet' THEN 'balance'
                    WHEN statement = 'cash_flows' THEN 'cashflow'
                    WHEN statement = 'ratios' THEN 'ratios'
                    WHEN statement = 'share_holding' THEN 'share_holding'
                    ELSE statement
                END as statement,
                'screener' as source,
                consolidation,
                COUNT(DISTINCT period_end) as period_count,
                MIN(period_end) as min_period_end,
                MAX(period_end) as max_period_end
            FROM financials_screener
            WHERE symbol IN ({placeholders})
            GROUP BY symbol, period_type, statement, consolidation
        """
        cursor.execute(query_scr, symbols)
        rows_scr = cursor.fetchall()
        
        all_rows = rows_yf + rows_scr
        if not all_rows:
            return pl.DataFrame(schema={
                "symbol": pl.String,
                "period_type": pl.String,
                "statement": pl.String,
                "source": pl.String,
                "consolidation": pl.String,
                "period_count": pl.Int64,
                "min_period_end": pl.Date,
                "max_period_end": pl.Date,
                "years_covered": pl.Float64
            })
        df = pl.DataFrame(all_rows, schema=[
            "symbol", "period_type", "statement", "source", "consolidation",
            "period_count", "min_period_end", "max_period_end"
        ], orient="row")
        df = df.with_columns(
            pl.col("min_period_end").str.to_date("%Y-%m-%d"),
            pl.col("max_period_end").str.to_date("%Y-%m-%d"),
        )
    finally:
        con.close()

    df = df.with_columns(
        ((pl.col("max_period_end") - pl.col("min_period_end")).dt.total_days() / 365.25).alias("years_covered")
    )
    return df


def get_ohlcv_coverage(symbols: list[str]) -> pl.DataFrame:
    if not symbols:
        return pl.DataFrame(schema={
            "symbol": pl.String,
            "min_ts": pl.Datetime,
            "max_ts": pl.Datetime,
            "row_count": pl.Int64
        })

    with market_db.get_connection() as con:
        placeholders = ",".join("?" * len(symbols))
        query = f"""
            SELECT 
                symbol,
                MIN(ts) as min_ts,
                MAX(ts) as max_ts,
                COUNT(*) as row_count
            FROM ohlcv
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        """
        df = con.execute(query, symbols).pl()

    if df.is_empty():
        return pl.DataFrame(schema={
            "symbol": pl.String,
            "min_ts": pl.Datetime,
            "max_ts": pl.Datetime,
            "row_count": pl.Int64
        })

    return df


def compute_coverage_report(min_quarters_per_year: int = 4) -> dict:
    mainboard = get_mainboard_symbols()
    operating = mainboard.filter(pl.col("is_operating_company"))
    op_symbols = operating["symbol"].to_list()

    fin_cov = get_financial_coverage(op_symbols)
    ohlcv_cov = get_ohlcv_coverage(op_symbols)

    stmt_periods = ["income", "balance", "cashflow"]
    period_types = ["annual", "quarterly"]
    sources = ["screener", "yfinance"]

    # Pivot by source as well to get per-source coverage
    fin_pivot = fin_cov.pivot(
        values=["period_count", "min_period_end", "max_period_end", "years_covered"],
        index="symbol",
        on=["period_type", "statement", "source"],
        aggregate_function="first"
    )

    # Pivot creates columns like: period_count_{"annual","income","screener"}
    # Rename them to simpler names
    rename_map = {}
    for pt in period_types:
        for stmt in stmt_periods:
            for src in sources:
                old_pc = f'period_count_{{\"{pt}\",\"{stmt}\",\"{src}\"}}'
                old_min = f'min_period_end_{{\"{pt}\",\"{stmt}\",\"{src}\"}}'
                old_max = f'max_period_end_{{\"{pt}\",\"{stmt}\",\"{src}\"}}'
                old_yrs = f'years_covered_{{\"{pt}\",\"{stmt}\",\"{src}\"}}'
                new_pc = f"period_count_{pt}_{stmt}_{src}"
                new_min = f"min_period_end_{pt}_{stmt}_{src}"
                new_max = f"max_period_end_{pt}_{stmt}_{src}"
                new_yrs = f"years_covered_{pt}_{stmt}_{src}"
                if old_pc in fin_pivot.columns:
                    rename_map[old_pc] = new_pc
                if old_min in fin_pivot.columns:
                    rename_map[old_min] = new_min
                if old_max in fin_pivot.columns:
                    rename_map[old_max] = new_max
                if old_yrs in fin_pivot.columns:
                    rename_map[old_yrs] = new_yrs
    
    if rename_map:
        fin_pivot = fin_pivot.rename(rename_map)

    def get_col(base: str, pt: str, stmt: str, src: str = "") -> str:
        if src:
            return f"{base}_{pt}_{stmt}_{src}"
        return f"{base}_{pt}_{stmt}"

    for pt in period_types:
        for stmt in stmt_periods:
            for src in sources:
                pc_col = get_col("period_count", pt, stmt, src)
                if pc_col not in fin_pivot.columns:
                    fin_pivot = fin_pivot.with_columns(pl.lit(0).alias(pc_col))

    # Aggregate across sources (prefer screener, fallback to yfinance)
    for pt in period_types:
        for stmt in stmt_periods:
            screener_col = get_col("period_count", pt, stmt, "screener")
            yfinance_col = get_col("period_count", pt, stmt, "yfinance")
            fin_pivot = fin_pivot.with_columns(
                pl.coalesce([pl.col(screener_col), pl.col(yfinance_col)]).alias(get_col("period_count", pt, stmt)),
            )

    annual_cols = [get_col("period_count", "annual", s) for s in stmt_periods]
    quarterly_cols = [get_col("period_count", "quarterly", s) for s in stmt_periods]

    fin_pivot = fin_pivot.with_columns(
        pl.sum_horizontal([pl.col(c).fill_null(0) for c in annual_cols]).alias("annual_periods_total"),
        pl.sum_horizontal([pl.col(c).fill_null(0) for c in quarterly_cols]).alias("quarterly_periods_total"),
    )

    for stmt in stmt_periods:
        fin_pivot = fin_pivot.with_columns(
            (pl.col(get_col("period_count", "annual", stmt)) > 0).alias(f"has_{stmt}_annual"),
            (pl.col(get_col("period_count", "quarterly", stmt)) > 0).alias(f"has_{stmt}_quarterly"),
        )

    fin_pivot = fin_pivot.with_columns(
        pl.col(get_col("max_period_end", "annual", "income", "screener")).alias("annual_max_date"),
        pl.col(get_col("min_period_end", "annual", "income", "screener")).alias("annual_min_date"),
        pl.when(pl.col(get_col("max_period_end", "quarterly", "income", "screener")).is_not_null())
        .then(pl.col(get_col("max_period_end", "quarterly", "income", "screener")))
        .otherwise(pl.col(get_col("max_period_end", "quarterly", "income", "yfinance")))
        .alias("quarterly_max_date"),
        pl.when(pl.col(get_col("min_period_end", "quarterly", "income", "screener")).is_not_null())
        .then(pl.col(get_col("min_period_end", "quarterly", "income", "screener")))
        .otherwise(pl.col(get_col("min_period_end", "quarterly", "income", "yfinance")))
        .alias("quarterly_min_date"),
    )

    fin_pivot = fin_pivot.with_columns(
        pl.when(pl.col("annual_max_date").is_not_null() & pl.col("annual_min_date").is_not_null())
        .then((pl.col("annual_max_date") - pl.col("annual_min_date")).dt.total_days() / 365.25)
        .otherwise(0)
        .alias("annual_years_covered"),
        pl.when(pl.col("quarterly_max_date").is_not_null() & pl.col("quarterly_min_date").is_not_null())
        .then((pl.col("quarterly_max_date") - pl.col("quarterly_min_date")).dt.total_days() / 365.25)
        .otherwise(0)
        .alias("quarterly_years_covered"),
    )

    fin_pivot = fin_pivot.with_columns(
        pl.when(pl.col("quarterly_years_covered") > 0)
        .then(pl.col("quarterly_periods_total") / pl.col("quarterly_years_covered"))
        .otherwise(0)
        .alias("quarters_per_year_avg"),
    )

    fin_pivot = fin_pivot.with_columns(
        (pl.col("quarters_per_year_avg") >= min_quarters_per_year).alias("meets_quarterly_threshold"),
    )

    ohlcv_cov = ohlcv_cov.with_columns(
        pl.col("min_ts").cast(pl.Date).alias("ohlcv_first"),
        pl.col("max_ts").cast(pl.Date).alias("ohlcv_last"),
    )

    result = operating.join(fin_pivot, on="symbol", how="left").join(ohlcv_cov, on="symbol", how="left")

    result = result.with_columns(
        pl.col(get_col("period_count", "annual", "income")).fill_null(0).alias("annual_periods"),
        pl.col(get_col("period_count", "quarterly", "income")).fill_null(0).alias("quarterly_periods"),
        pl.col("has_income_annual").fill_null(False),
        pl.col("has_balance_annual").fill_null(False),
        pl.col("has_cashflow_annual").fill_null(False),
        pl.col("has_income_quarterly").fill_null(False),
        pl.col("has_balance_quarterly").fill_null(False),
        pl.col("has_cashflow_quarterly").fill_null(False),
        pl.col("annual_max_date").fill_null(pl.lit(None)),
        pl.col("annual_min_date").fill_null(pl.lit(None)),
        pl.col("quarterly_max_date").fill_null(pl.lit(None)),
        pl.col("quarterly_min_date").fill_null(pl.lit(None)),
        pl.col("annual_years_covered").fill_null(0),
        pl.col("quarterly_years_covered").fill_null(0),
        pl.col("quarters_per_year_avg").fill_null(0),
        pl.col("meets_quarterly_threshold").fill_null(False),
        pl.col("ohlcv_first").fill_null(pl.lit(None)),
        pl.col("ohlcv_last").fill_null(pl.lit(None)),
        pl.col("row_count").fill_null(0).alias("ohlcv_rows"),
    )

    result = result.with_columns(
        pl.when(pl.col("annual_min_date").is_not_null() & pl.col("ohlcv_first").is_not_null())
        .then((pl.col("annual_min_date") - pl.col("ohlcv_first")).dt.total_days() / 365.25)
        .otherwise(0)
        .alias("gap_years_before_financials"),
    )

    has_fin = (pl.col("annual_periods") > 0) | (pl.col("quarterly_periods") > 0)
    result = result.with_columns(has_fin.alias("has_financials"))

    by_symbol = result.select([
        "symbol", "name", "series", "listed_on", "last_seen",
        "has_financials",
        "annual_periods", "annual_min_date", "annual_max_date", "annual_years_covered",
        "quarterly_periods", "quarterly_min_date", "quarterly_max_date", "quarterly_years_covered",
        "quarters_per_year_avg", "meets_quarterly_threshold",
        "has_income_annual", "has_balance_annual", "has_cashflow_annual",
        "has_income_quarterly", "has_balance_quarterly", "has_cashflow_quarterly",
        "ohlcv_first", "ohlcv_last", "ohlcv_rows",
        "gap_years_before_financials",
    ]).sort("symbol")

    total_mainboard = mainboard.height
    total_operating = operating.height
    total_with_fin = by_symbol.filter(pl.col("has_financials")).height
    total_meet_q = by_symbol.filter(pl.col("meets_quarterly_threshold")).height

    stmt_cov = {}
    for stmt in stmt_periods:
        for pt in period_types:
            col = f"has_{stmt}_{pt}"
            if col in by_symbol.columns:
                cnt = by_symbol.filter(pl.col(col)).height
                stmt_cov[f"{stmt}_{pt}"] = {"count": cnt, "pct": round(cnt / total_operating * 100, 1)}

    no_fin = by_symbol.filter(~pl.col("has_financials")).sort("ohlcv_first")
    top_gaps = no_fin.head(20).select(["symbol", "name", "ohlcv_first", "ohlcv_last"]).to_dicts()

    summary = {
        "total_mainboard_symbols": total_mainboard,
        "total_operating_companies": total_operating,
        "symbols_with_financials": total_with_fin,
        "coverage_pct": round(total_with_fin / total_operating * 100, 1) if total_operating else 0,
        "symbols_meeting_quarterly_threshold": total_meet_q,
        "quarterly_threshold_pct": round(total_meet_q / total_operating * 100, 1) if total_operating else 0,
        "statement_coverage": stmt_cov,
        "avg_annual_periods": round(by_symbol.filter(pl.col("has_financials"))["annual_periods"].mean(), 1) if total_with_fin else 0,
        "avg_quarterly_periods": round(by_symbol.filter(pl.col("has_financials"))["quarterly_periods"].mean(), 1) if total_with_fin else 0,
        "avg_quarters_per_year": round(by_symbol.filter(pl.col("has_financials"))["quarters_per_year_avg"].mean(), 1) if total_with_fin else 0,
        "avg_gap_years": round(no_fin["gap_years_before_financials"].mean(), 1) if no_fin.height else 0,
        "top_gaps": top_gaps,
    }

    return {
        "summary": summary,
        "by_symbol": by_symbol.to_dicts(),
        "mainboard_full": mainboard.to_dicts(),
    }


def export_csv(report: dict, path: Path) -> None:
    df = pl.DataFrame(report["by_symbol"])
    df.write_csv(path)


def export_json(report: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)


def print_console_summary(report: dict) -> None:
    s = report["summary"]
    print("\n" + "=" * 60)
    print("FINANCIAL DATA COVERAGE REPORT — MAINBOARD OPERATING COMPANIES")
    print("=" * 60)
    print(f"Generated: {date.today()}")
    print(f"Quarterly threshold: ≥4 quarters/year\n")

    print("OVERALL")
    print("-" * 60)
    print(f"Mainboard symbols (EQ/BE/BZ, stocks):     {s['total_mainboard_symbols']:,}")
    print(f"Operating companies (after filtering):     {s['total_operating_companies']:,}")
    print(f"Symbols with any financials:               {s['symbols_with_financials']:,} ({s['coverage_pct']:.1f}%)")
    print(f"Symbols meeting quarterly threshold:       {s['symbols_meeting_quarterly_threshold']:,} ({s['quarterly_threshold_pct']:.1f}%)")

    print("\nANNUAL COVERAGE (symbols with financials)")
    print("-" * 60)
    print(f"Avg annual periods per symbol:             {s['avg_annual_periods']:.1f}")
    for stmt in ["income", "balance", "cashflow"]:
        key = f"{stmt}_annual"
        if key in s["statement_coverage"]:
            c = s["statement_coverage"][key]
            print(f"{stmt.capitalize()} statement coverage:               {c['count']:,} ({c['pct']:.1f}%)")

    print("\nQUARTERLY COVERAGE (symbols with financials)")
    print("-" * 60)
    print(f"Avg quarterly periods per symbol:          {s['avg_quarterly_periods']:.1f}")
    print(f"Avg quarters per year:                     {s['avg_quarters_per_year']:.1f}")
    for stmt in ["income", "balance", "cashflow"]:
        key = f"{stmt}_quarterly"
        if key in s["statement_coverage"]:
            c = s["statement_coverage"][key]
            print(f"{stmt.capitalize()} statement coverage:               {c['count']:,} ({c['pct']:.1f}%)")

    print("\nOHLCV vs FINANCIALS GAP")
    print("-" * 60)
    no_fin_count = s["total_operating_companies"] - s["symbols_with_financials"]
    print(f"Symbols with OHLCV but NO financials:      {no_fin_count:,}")
    print(f"Avg OHLCV years before first financial:    {s['avg_gap_years']:.1f} years")

    print("\nTOP GAPS (OHLCV history predating financials by most years)")
    print("-" * 60)
    for i, g in enumerate(s["top_gaps"][:15], 1):
        ohlcv_first = g["ohlcv_first"] or "N/A"
        ohlcv_last = g["ohlcv_last"] or "N/A"
        print(f"{i:2d}. {g['symbol']:<15} — OHLCV: {ohlcv_first} to {ohlcv_last}")

    print("\n" + "=" * 60)


def get_operating_without_financials_csv() -> str:
    """Generate CSV of operating companies without financials."""
    report = compute_coverage_report()
    by_symbol = pl.DataFrame(report["by_symbol"])
    no_fin = by_symbol.filter(~pl.col("has_financials")).sort("symbol")
    
    output_cols = [
        "symbol", "name", "series", "listed_on", "last_seen",
        "ohlcv_first", "ohlcv_last", "ohlcv_rows", "gap_years_before_financials"
    ]
    return no_fin.select(output_cols).write_csv()


if __name__ == "__main__":
    report = compute_coverage_report()
    print_console_summary(report)
    
    csv_output = get_operating_without_financials_csv()
    print(csv_output)