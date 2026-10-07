from dataclasses import asdict
import time
from datetime import date, timedelta
from typing import Optional

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import or_

from app.config import settings
from app.engine import adjust
from app.engine import allocation, backtest, db as market_db
from app.engine import fundamentals, screener
from app.engine.allocation import AllocationConfig
from app.engine.screener import UnknownMetricError
from app.state.schemas import AllocationBacktestRequest, BacktestRequest, ScreenerRequest

router = APIRouter(prefix="/api/market", tags=["market"])

_SCREENER_TTL_SECONDS = 300.0
_screener_cache: tuple[float, pl.DataFrame] | None = None


def _securities_frame() -> pl.DataFrame:
    from sqlalchemy.orm import Session

    from app.state.db import SessionLocal
    from app.state.models import Security, SecurityInfo

    with SessionLocal() as session:
        secs = pl.DataFrame(
            session.query(Security.symbol, Security.name, Security.instrument_type)
            .order_by(Security.symbol)
            .all(),
            schema={"symbol": pl.String, "name": pl.String, "instrument_type": pl.String},
            orient="row",
        )
        infos = pl.DataFrame(
            session.query(
                SecurityInfo.symbol,
                SecurityInfo.sector,
                SecurityInfo.industry,
                SecurityInfo.shares_outstanding,
            ).all(),
            schema={
                "symbol": pl.String,
                "sector": pl.String,
                "industry": pl.String,
                "shares_outstanding": pl.Float64,
            },
            orient="row",
        )
    return secs.join(infos, on="symbol", how="left")


def _screener_frame() -> pl.DataFrame:
    """Snapshot + fundamentals + securities, cached in-process for a few minutes."""
    global _screener_cache
    now = time.monotonic()
    if _screener_cache is not None and now - _screener_cache[0] < _SCREENER_TTL_SECONDS:
        return _screener_cache[1]
    snapshot = market_db.fetch_latest_snapshot().select("symbol", "close", "last_ts")
    shares = _securities_frame().select("symbol", "shares_outstanding")
    metrics = fundamentals.compute_universe_metrics(
        fundamentals.pivot_financials_unified(fundamentals.load_financials_unified()),
        prices=snapshot,
        shares_override=shares,
    )
    frame = metrics.join(
        market_db.fetch_latest_snapshot().select("symbol", "close", "volume"), on="symbol", how="left"
    )
    frame = frame.join(_securities_frame(), on="symbol", how="left")
    _screener_cache = (now, frame)
    return frame


@router.get("/indices")
async def get_indices(max_age_days: int = 7) -> list[dict]:
    """All index series with coverage and a freshness flag.

    The DuckDB `indices` table holds retired NSE series kept for long-run
    history, so `is_live` marks the ones the daily updater still maintains.
    """
    df = market_db.index_summary(max_age_days=max_age_days)
    return df.to_dicts()


@router.get("/indices/{symbol}")
async def get_index_close(
    symbol: str, start: str | None = None, end: str | None = None
) -> list[dict]:
    df = market_db.fetch_indices(symbol.upper(), start=start, end=end)
    return df.to_dicts()


@router.get("/ohlcv/{symbol}")
async def get_ohlcv(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 5000,
    adjusted: bool = False,
) -> list[dict]:
    df = market_db.fetch_ohlcv(symbol.upper(), start=start, end=end, limit=limit)
    if adjusted:
        df = adjust.apply_corporate_adjustments(df, adjust.load_actions([symbol.upper()]))
    return df.select("symbol", "ts", "open", "high", "low", "close", "volume").to_dicts()


def compute_category(series: str | None, instrument_type: str | None, is_active: bool, name: str | None) -> str:
    """Compute UI category tag from security attributes."""
    if not is_active:
        return "delisted"
    if series in ("SM", "ST", "SZ"):
        return "sme"
    if instrument_type == "etf":
        return "etf"
    if instrument_type == "mf":
        if not name:
            return "mf"
        n = name.upper()
        if "LIQUID" in n or "OVERNIGHT" in n or "ULTRA SHORT" in n or "MONEY MARKET" in n:
            return "mf-debt"
        if "ELSS" in n or "TAX SAVER" in n or "EQUITY" in n or "LARGE CAP" in n or "MID CAP" in n or "SMALL CAP" in n or "FLEXI CAP" in n or "MULTI CAP" in n or "FOCUSED" in n or "VALUE" in n or "SECTORAL" in n or "THEMATIC" in n or "PHARMA" in n or "BANKING" in n or "TECHNOLOGY" in n or "HEALTHCARE" in n or "INFRASTRUCTURE" in n or "CONSUMPTION" in n or "FINANCIAL" in n or "ENERGY" in n:
            return "mf-equity"
        if "ARBITRAGE" in n or "BALANCED ADVANTAGE" in n or "DYNAMIC ASSET" in n or "DYNAMIC TERM" in n or "HYBRID" in n or "BALANCED" in n or "MULTI ASSET" in n or "ASSET ALLOCATION" in n:
            return "mf-hybrid"
        if "INDEX" in n and ("FUND" in n or "ETF" in n):
            return "mf-index"
        if "DEBT" in n or "CORPORATE BOND" in n or "GILT" in n or "CREDIT RISK" in n or "BANKING & PSU" in n or "BANKING AND PSU" in n or "FLOATER" in n or "DURATION" in n or "INCOME" in n:
            return "mf-debt"
        return "mf"
    if series in ("EQ", "BE", "BZ"):
        if name:
            n = name.upper()
            if "REIT" in n:
                return "reit"
            if "INVIT" in n or "INFRASTRUCTURE INVESTMENT" in n:
                return "invit"
        return "equity"
    if series == "MF":
        return "mf"
    return "other"


@router.get("/tickers")
async def search_tickers(
    q: str = "",
    types: str | None = None,
    limit: int = 20,
    exclude_sme: bool = True,
    exclude_delisted: bool = True,
) -> list[dict]:
    """Search tradeable tickers (stocks, ETFs, MFs, indices) by symbol or name.

    Backed by the SQLite securities master plus DuckDB indices; per-symbol
    date_first_entry/date_last_entry come from a TTL-cached coverage scan.
    """
    q = q.strip()
    ql = q.lower()
    type_filter = {t.strip().lower() for t in (types or "").split(",") if t.strip()} or None
    limit = max(1, min(limit, 100))

    want = type_filter or {"stock", "etf", "mf", "index"}
    results: list[dict] = []

    if want - {"index"}:
        from app.state.db import SessionLocal
        from app.state.models import Security

        with SessionLocal() as session:
            query = session.query(
                Security.symbol, Security.name, Security.instrument_type, Security.is_active, Security.series
            )
            if q:
                like = f"%{q}%"
                query = query.filter(or_(Security.symbol.ilike(like), Security.name.ilike(like)))
            
            # Filter by instrument_type at DB level to avoid 2000-row limit bias
            if want == {"mf"}:
                query = query.filter(Security.instrument_type == "mf")
            elif want == {"etf"}:
                query = query.filter(Security.instrument_type == "etf")
            elif want == {"stock"}:
                query = query.filter(Security.instrument_type == "stock")
            elif want - {"mf", "etf"} == {"stock"}:
                query = query.filter(Security.instrument_type.in_(["stock", "mf", "etf"]))
            
            # Order by is_active desc (active first), then instrument_type, then symbol for balanced results
            query = query.order_by(Security.is_active.desc(), Security.instrument_type, Security.symbol)
            rows = query.limit(2000).all()
        sec_types = {"mf", "etf"}
        for symbol, name, instrument_type, is_active, series in rows:
            ttype = instrument_type if instrument_type in sec_types else "stock"
            if ttype not in want:
                continue
            category = compute_category(series, instrument_type, bool(is_active), name)
            if exclude_sme and category == "sme":
                continue
            if exclude_delisted and category == "delisted":
                continue
            results.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "type": ttype,
                    "category": category,
                    "is_active": bool(is_active),
                }
            )

    if "index" in want:
        for symbol in market_db.list_indices():
            if ql and ql not in symbol.lower():
                continue
            results.append({"symbol": symbol, "name": None, "type": "index", "category": "index", "is_active": True})

    coverage = {
        row["symbol"]: (row["date_first_entry"], row["date_last_entry"])
        for row in market_db.cached_coverage().to_dicts()
    }

    def rank(r: dict) -> tuple:
        sym = str(r["symbol"]).lower()
        if not ql:
            exact = 3
        elif sym == ql:
            exact = 0
        elif sym.startswith(ql):
            exact = 1
        else:
            exact = 2
        first, last = coverage.get(r["symbol"], (None, None))
        recency = last.timestamp() if last is not None else 0.0
        return (exact, -recency, sym)

    results.sort(key=rank)
    results = results[:limit]
    for r in results:
        first, last = coverage.get(r["symbol"], (None, None))
        r["date_first_entry"] = first.date().isoformat() if first is not None else None
        r["date_last_entry"] = last.date().isoformat() if last is not None else None
    return results


_RETURN_WINDOWS: dict[str, int] = {"1y": 365, "3y": 3 * 365, "5y": 5 * 365, "10y": 10 * 365}


def _trailing_returns(df: pl.DataFrame) -> dict[str, float | None]:
    """Trailing returns from daily closes: YTD/1Y simple, 3Y/5Y/10Y CAGR."""
    out: dict[str, float | None] = {"ytd": None, "1y": None, "3y": None, "5y": None, "10y": None}
    if df.is_empty() or len(df) < 2:
        return out
    df = df.sort("ts")
    last_ts = df["ts"][-1]
    last_close = float(df["close"][-1])
    if last_close <= 0:
        return out

    def _window_return(cutoff, annualized: bool) -> float | None:
        wdf = df.filter(pl.col("ts") >= cutoff) if cutoff is not None else df
        if wdf.is_empty():
            return None
        start_close = float(wdf["close"][0])
        if start_close <= 0:
            return None
        change = last_close / start_close - 1.0
        if not annualized:
            return change
        years = (last_ts - wdf["ts"][0]).days / 365.25
        if years <= 0:
            return None
        return (last_close / start_close) ** (1.0 / years) - 1.0

    ytd_start = last_ts.replace(month=1, day=1)
    out["ytd"] = _window_return(ytd_start, annualized=False)
    for window, days in _RETURN_WINDOWS.items():
        annualized = days > 365
        out[window] = _window_return(last_ts - timedelta(days=days), annualized)
    return out


@router.get("/overview/{symbol}")
async def get_overview(symbol: str) -> dict:
    """Type-aware latest info for one ticker: price/NAV + key metrics + trailing returns."""
    symbol = symbol.upper()
    from app.state.db import SessionLocal
    from app.state.models import Security

    with SessionLocal() as session:
        sec = session.query(Security).filter_by(symbol=symbol).first()
    instrument_type = (sec.instrument_type if sec else None) or ""
    if symbol in set(market_db.list_indices()):
        ttype = "index"
    elif instrument_type == "mf":
        ttype = "mf"
    elif instrument_type == "etf":
        ttype = "etf"
    else:
        ttype = "stock"

    if ttype == "index":
        df = market_db.fetch_indices(symbol, limit=100_000)
    else:
        df = market_db.fetch_ohlcv(symbol, limit=100_000)
    price = float(df["close"][-1]) if not df.is_empty() else None
    last_ts = df["ts"][-1].isoformat() if not df.is_empty() else None

    out: dict = {
        "symbol": symbol,
        "name": sec.name if sec else None,
        "type": ttype,
        "price": price,
        "last_ts": last_ts,
        "returns": _trailing_returns(df),
        "metrics": {},
    }

    if ttype in ("stock", "etf"):
        snapshot = market_db.fetch_latest_snapshot().select("symbol", "close", "last_ts")
        shares = _securities_frame().select("symbol", "shares_outstanding")
        fin_piv = fundamentals.pivot_financials_unified(fundamentals.load_financials_unified([symbol]))
        metrics = fundamentals.compute_universe_metrics(
            fin_piv, prices=snapshot, shares_override=shares
        )
        if not metrics.is_empty():
            row = metrics.to_dicts()[0]
            out["metrics"] = {
                key: {
                    "value": row.get(key),
                    "source": "screener" if key in ("revenue", "net_income", "ebitda", "free_cash_flow", "equity", "total_assets", "total_debt") else "yfinance",
                    "consolidation": settings.financials_consolidation,
                }
                for key in (
                    "market_cap",
                    "pe",
                    "pb",
                    "roe",
                    "roce",
                    "debt_to_equity",
                    "net_margin",
                    "operating_margin",
                    "revenue",
                    "net_income",
                    "revenue_growth",
                    "earnings_growth",
                )
            }
    return out


@router.get("/close-on")
async def get_close_on(symbol: str, day: date) -> dict:
    """Close on the exact date, else the nearest previous trading day."""
    symbol = symbol.upper()
    close = market_db.fetch_close_on(symbol, day)
    if close is None:
        with market_db.get_connection() as con:
            row = con.execute(
                "SELECT close FROM indices WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                [symbol, day],
            ).fetchone()
        close = float(row[0]) if row and row[0] is not None else None
    return {"symbol": symbol, "day": day.isoformat(), "close": close}


def _daily_prices(symbols: list[str], start: str, end: str) -> tuple[pl.DataFrame, list[str]]:
    """Adjusted daily closes per symbol, plus the list of symbols with no data."""
    frames: list[pl.DataFrame] = []
    missing: list[str] = []
    for symbol in symbols:
        df = market_db.fetch_ohlcv(symbol, start=start, end=end, limit=100_000)
        if df.is_empty():
            missing.append(symbol)
            continue
        df = adjust.apply_corporate_adjustments(df, adjust.load_actions([symbol]))
        frames.append(df.select("symbol", "ts", "close"))
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.String, "ts": pl.Datetime, "close": pl.Float64}), missing
    return pl.concat(frames), missing


@router.post("/backtest-allocation")
async def backtest_allocation_endpoint(req: AllocationBacktestRequest) -> dict:
    start = (req.start or date.today().replace(year=date.today().year - 10)).isoformat()
    end = (req.end or date.today()).isoformat()
    symbols = [s.strip().upper() for s in req.symbols if s.strip()]

    daily, missing = _daily_prices(symbols, start, end)
    all_missing = [s for s in symbols if s in missing]
    if len(missing) == len(symbols):
        raise HTTPException(
            status_code=422,
            detail=f"no price data for any symbol in {start}..{end}: {all_missing}",
        )
    matrix = allocation.monthly_close_matrix(daily)
    available = [s for s in symbols if s in matrix.columns]

    cfg = AllocationConfig(
        symbols=available,
        weights=None
        if req.weights is None
        else [req.weights[symbols.index(s)] if s in symbols else 0.0 for s in available],
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        initial_capital=req.initial_capital,
        monthly_contribution=req.monthly_contribution,
        annual_step_up_pct=req.annual_step_up_pct,
        rebalance_freq=req.rebalance_freq,
    )
    strategy_cashflows: list[tuple[date, float]] = []
    strategy = allocation.simulate_allocation(matrix, cfg, cashflows=strategy_cashflows)
    if len(strategy["curve"]) < 2:
        raise HTTPException(
            status_code=422,
            detail=f"not enough price data for the requested range (symbols with data: {available})",
        )

    benchmark_data: dict | None = None
    bench_name = (req.benchmark or "").upper()
    if bench_name:
        bench_daily = market_db.fetch_indices(bench_name, start=start, end=end)
        if not bench_daily.is_empty():
            bench_matrix = allocation.monthly_close_matrix(
                bench_daily.rename({"symbol": "symbol", "ts": "ts", "close": "close"})
            )
            bench_symbols = [c for c in bench_matrix.columns if c != "month"]
            bench_cfg = AllocationConfig(
                symbols=bench_symbols,
                weights=[1.0],
                start=date.fromisoformat(start),
                end=date.fromisoformat(end),
                initial_capital=req.initial_capital,
                monthly_contribution=req.monthly_contribution,
                annual_step_up_pct=req.annual_step_up_pct,
            )
            bench_cashflows: list[tuple[date, float]] = []
            bench_result = allocation.simulate_allocation(
                bench_matrix, bench_cfg, cashflows=bench_cashflows
            )
            if bench_result["curve"]:
                benchmark_data = {
                    "symbol": bench_name,
                    "curve": bench_result["curve"],
                    "metrics": allocation.performance_metrics(
                        bench_result["curve"], bench_cashflows
                    ),
                }

    return {
        "symbols": available,
        "missing_symbols": missing,
        "strategy": {
            "curve": strategy["curve"],
            "metrics": allocation.performance_metrics(strategy["curve"], strategy_cashflows),
        },
        "benchmark": benchmark_data,
    }


@router.post("/screener")
async def screener_endpoint(req: ScreenerRequest) -> dict:
    frame = _screener_frame()
    if frame.is_empty():
        return {"total": 0, "rows": []}
    try:
        return screener.run_screen(
            frame,
            [f.model_dump() for f in req.filters],
            rank_by=req.rank_by,
            rank_desc=req.rank_desc,
            limit=req.limit,
            instrument_types=req.instrument_types,
        )
    except UnknownMetricError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/fundamentals/{symbol}")
async def get_fundamentals(
    symbol: str,
    consolidation: str = Query(default="consolidated", pattern="^(consolidated|standalone)$"),
) -> dict:
    symbol = symbol.upper()
    snapshot = market_db.fetch_latest_snapshot().select("symbol", "close", "last_ts")
    prices = (
        market_db.fetch_ohlcv(symbol, limit=100_000)
        .select(pl.col("ts"), pl.col("close"))
        .sort("ts")
    )
    fin_piv = fundamentals.pivot_financials_unified(fundamentals.load_financials_unified([symbol], consolidation=consolidation))
    latest = fundamentals.compute_universe_metrics(fin_piv, prices=snapshot)
    latest_row = latest.to_dicts()[0] if not latest.is_empty() else None
    history = fundamentals.compute_symbol_history(fin_piv, prices)
    
    # Add source/consolidation info to latest metrics
    if latest_row:
        for key in latest_row:
            if latest_row[key] is not None:
                latest_row[key] = {
                    "value": latest_row[key],
                    "source": "screener" if key in ("revenue", "net_income", "ebitda", "free_cash_flow", "equity", "total_assets", "total_debt", "shares") else "yfinance",
                    "consolidation": consolidation,
                }
    
    return {"symbol": symbol, "latest": latest_row, "history": history, "consolidation": consolidation}


@router.get("/fundamentals/{symbol}/statements")
async def get_statements(
    symbol: str,
    statement: str | None = None,
    consolidation: str = Query(default="consolidated", pattern="^(consolidated|standalone)$"),
) -> dict:
    symbol = symbol.upper()
    df = fundamentals.load_financials_unified([symbol], consolidation=consolidation)
    if statement is not None:
        df = df.filter(pl.col("statement") == statement)
    if df.is_empty():
        return {"symbol": symbol, "statements": {}}
    out: dict[str, list[dict]] = {}
    for stmt_key, group in df.group_by("statement"):
        stmt = stmt_key[0] if isinstance(stmt_key, tuple) else stmt_key
        rows = (
            group.sort("period_end")
            .select("period_end", "period_type", "line_item", "value", "source", "consolidation")
            .to_dicts()
        )
        # Convert value to Cr for display
        for row in rows:
            if row["value"] is not None:
                row["value"] = row["value"] / 10_000_000  # Convert to Cr
        out[str(stmt)] = rows
    return {"symbol": symbol, "statements": out, "consolidation": consolidation}


@router.post("/backtest")
async def backtest_endpoint(req: BacktestRequest) -> dict:
    df = market_db.fetch_ohlcv(req.symbol.upper())
    try:
        result = backtest.run_backtest(
            df,
            backtest.BacktestConfig(
                symbol=req.symbol.upper(),
                fast=req.fast,
                slow=req.slow,
                initial_capital=req.initial_capital,
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return asdict(result)
