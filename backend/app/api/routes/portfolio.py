from dataclasses import asdict
import csv
import io
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine import db as market_db
from app.engine import rebalance, wealth
from app.state import models
from app.state.schemas import (
    ComputedPosition,
    DividendCreate,
    DividendRead,
    PortfolioCreate,
    PortfolioRead,
    PortfolioSummary,
    RebalanceRequest,
    RebalanceRules,
    TransactionCreate,
    TransactionRead,
    WatchlistItemCreate,
    WatchlistItemRead,
)

router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])
watchlist_router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def _get_portfolio_or_404(portfolio_id: int, db: Session) -> models.Portfolio:
    portfolio = db.get(models.Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="portfolio not found")
    return portfolio


@router.get("", response_model=list[PortfolioRead])
def list_portfolios(db: Session = Depends(get_db)) -> list[models.Portfolio]:
    return db.query(models.Portfolio).order_by(models.Portfolio.name).all()


@router.post("", response_model=PortfolioRead, status_code=201)
def create_portfolio(body: PortfolioCreate, db: Session = Depends(get_db)) -> models.Portfolio:
    existing = db.query(models.Portfolio).filter_by(name=body.name).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"portfolio '{body.name}' already exists")
    portfolio = models.Portfolio(
        name=body.name,
        base_currency=body.base_currency,
        rebalancing_rules=body.rebalancing_rules,
    )
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)
    return portfolio


@router.get("/{portfolio_id}", response_model=PortfolioRead)
def get_portfolio(portfolio_id: int, db: Session = Depends(get_db)) -> models.Portfolio:
    return _get_portfolio_or_404(portfolio_id, db)


@router.delete("/{portfolio_id}", status_code=204)
def delete_portfolio(portfolio_id: int, db: Session = Depends(get_db)) -> None:
    portfolio = _get_portfolio_or_404(portfolio_id, db)
    db.query(models.Transaction).filter_by(portfolio_id=portfolio_id).delete()
    db.query(models.Dividend).filter_by(portfolio_id=portfolio_id).delete()
    db.delete(portfolio)
    db.commit()


def compute_positions(transactions: list[models.Transaction]) -> list[ComputedPosition]:
    """Average-cost method: buys blend into the cost basis, sells reduce
    quantity at the current average cost and book realized P&L."""
    per_symbol: dict[str, list[models.Transaction]] = {}
    for txn in transactions:
        per_symbol.setdefault(txn.symbol, []).append(txn)

    out: list[ComputedPosition] = []
    for symbol, txns in per_symbol.items():
        txns = sorted(txns, key=lambda t: (t.trade_date, t.id))
        qty_held = 0.0
        cost_basis = 0.0
        realized = 0.0
        for t in txns:
            if t.side == "buy":
                qty_held += t.quantity
                cost_basis += t.quantity * t.price + t.fees
            else:
                sold = min(t.quantity, qty_held)
                if qty_held > 0:
                    cost_removed = cost_basis * (sold / qty_held)
                    cost_basis -= cost_removed
                    realized += t.price * sold - cost_removed
                qty_held -= sold
        if qty_held <= 1e-9 and realized == 0:
            continue
        avg_cost = cost_basis / qty_held if qty_held > 1e-9 else 0.0
        out.append(
            ComputedPosition(
                symbol=symbol,
                isin=txns[-1].isin,
                quantity=round(qty_held, 6),
                avg_cost=round(avg_cost, 6),
                invested=round(avg_cost * qty_held, 2),
                realized=round(realized, 2),
            )
        )
    return sorted(out, key=lambda p: p.symbol)


@router.get("/{portfolio_id}/positions", response_model=list[ComputedPosition])
def get_positions(portfolio_id: int, db: Session = Depends(get_db)) -> list[ComputedPosition]:
    _get_portfolio_or_404(portfolio_id, db)
    transactions = (
        db.query(models.Transaction)
        .filter_by(portfolio_id=portfolio_id)
        .order_by(models.Transaction.trade_date, models.Transaction.id)
        .all()
    )
    return compute_positions(transactions)


@router.get("/{portfolio_id}/summary", response_model=PortfolioSummary)
def get_summary(portfolio_id: int, db: Session = Depends(get_db)) -> PortfolioSummary:
    _get_portfolio_or_404(portfolio_id, db)
    transactions = (
        db.query(models.Transaction)
        .filter_by(portfolio_id=portfolio_id)
        .order_by(models.Transaction.trade_date, models.Transaction.id)
        .all()
    )
    dividends = db.query(models.Dividend).filter_by(portfolio_id=portfolio_id).all()
    positions = compute_positions(transactions)

    snapshot_rows = market_db.fetch_latest_snapshot().select("symbol", "close", "last_ts").to_dicts()
    close_by_symbol = {r["symbol"]: r["close"] for r in snapshot_rows if r["close"] is not None}
    last_ts_by_symbol = {
        r["symbol"]: r["last_ts"] for r in snapshot_rows if r["last_ts"] is not None
    }

    holdings: list[dict] = []
    invested_total = current_total = unrealized_total = realized_total = 0.0
    for pos in positions:
        close = close_by_symbol.get(pos.symbol)
        value = pos.quantity * close if close is not None else None
        unrealized = value - pos.invested if value is not None else None
        holdings.append(
            {
                **pos.model_dump(),
                "close": close,
                "last_ts": (
                    last_ts_by_symbol[pos.symbol].isoformat()
                    if last_ts_by_symbol.get(pos.symbol) is not None
                    else None
                ),
                "current_value": value,
                "unrealized": unrealized,
                "unrealized_pct": (unrealized / pos.invested)
                if unrealized is not None and pos.invested > 0
                else None,
            }
        )
        invested_total += pos.invested
        realized_total += pos.realized
        if value is not None:
            current_total += value
            unrealized_total += unrealized if unrealized is not None else 0.0

    dividends_total = wealth.dividend_income(transactions, dividends)
    today = date.today()
    cashflows: list[tuple[date, float]] = []
    for t in transactions:
        if t.side == "buy":
            cashflows.append((t.trade_date, -(t.quantity * t.price + t.fees)))
        else:
            cashflows.append((t.trade_date, t.quantity * t.price - t.fees))
    for d in dividends:
        amount = d.total_amount
        if amount is None and d.amount_per_share is not None:
            amount = d.amount_per_share * wealth.held_quantity_at(transactions, d.symbol, d.ex_date)
        if amount:
            cashflows.append((d.ex_date, amount))
    if current_total > 0:
        cashflows.append((today, current_total))
    xirr = wealth.xirr(cashflows)

    return PortfolioSummary(
        holdings=holdings,
        invested=round(invested_total, 2),
        current_value=round(current_total, 2),
        unrealized=round(unrealized_total, 2),
        realized=round(realized_total, 2),
        dividends=round(dividends_total, 2),
        xirr=xirr,
    )


@router.get("/{portfolio_id}/gains")
def get_gains(portfolio_id: int, db: Session = Depends(get_db)) -> dict:
    _get_portfolio_or_404(portfolio_id, db)
    transactions = (
        db.query(models.Transaction)
        .filter_by(portfolio_id=portfolio_id)
        .order_by(models.Transaction.trade_date, models.Transaction.id)
        .all()
    )
    result = wealth.fifo_lots(transactions)
    return {
        "ltcg": round(result.ltcg, 2),
        "stcg": round(result.stcg, 2),
        "ltcg_tax_est": round(result.ltcg_tax, 2),
        "stcg_tax_est": round(result.stcg_tax, 2),
        "ltcg_exemption": wealth.LTCG_EXEMPTION,
        "ltcg_rate": wealth.LTCG_RATE,
        "stcg_rate": wealth.STCG_RATE,
        "lots": [
            {
                "symbol": lot.symbol,
                "buy_date": lot.buy_date.isoformat(),
                "sell_date": lot.sell_date.isoformat(),
                "quantity": lot.quantity,
                "buy_price": round(lot.buy_price, 4),
                "sell_price": lot.sell_price,
                "gain": round(lot.gain, 2),
                "holding_days": lot.holding_days,
                "term": lot.term,
            }
            for lot in result.lots
        ],
    }


@router.get("/{portfolio_id}/equity-curve")
def get_equity_curve(portfolio_id: int, db: Session = Depends(get_db)) -> list[dict]:
    _get_portfolio_or_404(portfolio_id, db)
    transactions = (
        db.query(models.Transaction)
        .filter_by(portfolio_id=portfolio_id)
        .order_by(models.Transaction.trade_date, models.Transaction.id)
        .all()
    )
    if not transactions:
        return []
    symbols = {t.symbol for t in transactions}
    prices = {s: market_db.fetch_ohlcv(s, limit=100_000) for s in symbols}
    return wealth.equity_curve(transactions, prices)


@router.put("/{portfolio_id}/rules", response_model=PortfolioRead)
def update_rules(
    portfolio_id: int, body: RebalanceRules, db: Session = Depends(get_db)
) -> models.Portfolio:
    portfolio = _get_portfolio_or_404(portfolio_id, db)
    portfolio.rebalancing_rules = body.model_dump()
    db.commit()
    db.refresh(portfolio)
    return portfolio


def _symbol_cagr(symbol: str, max_price_age_days: int = 120) -> float | None:
    """Historical CAGR from daily closes; None when data is short or stale."""
    df = market_db.fetch_ohlcv(symbol, limit=100_000)
    if len(df) < 756:  # ~3 years of trading days
        return None
    last_ts = df["ts"][-1]
    if (date.today() - last_ts.date()).days > max_price_age_days:
        return None
    first, last = float(df["close"][0]), float(df["close"][-1])
    if first <= 0 or last <= 0:
        return None
    years = (last_ts.date() - df["ts"][0].date()).days / 365.25
    if years < 3:
        return None
    return max(-0.5, min(0.5, (last / first) ** (1.0 / years) - 1.0))


@router.post("/{portfolio_id}/rebalance")
def rebalance_portfolio(
    portfolio_id: int, body: RebalanceRequest | None = None, db: Session = Depends(get_db)
) -> dict:
    _get_portfolio_or_404(portfolio_id, db)
    portfolio = db.get(models.Portfolio, portfolio_id)
    transactions = (
        db.query(models.Transaction)
        .filter_by(portfolio_id=portfolio_id)
        .order_by(models.Transaction.trade_date, models.Transaction.id)
        .all()
    )
    rules_data = (body.rules if body and body.rules is not None else None) or RebalanceRules(
        **(portfolio.rebalancing_rules or {})
    )
    positions = [p.model_dump() for p in compute_positions(transactions)]
    snapshot = (
        market_db.fetch_latest_snapshot().select("symbol", "close", "last_ts").to_dicts()
    )
    price_by_symbol = {
        r["symbol"]: r["close"] for r in snapshot if r["close"] is not None
    }

    txn_by_symbol: dict[str, list] = {}
    for t in transactions:
        txn_by_symbol.setdefault(t.symbol, []).append(t)

    result = rebalance.suggest_rebalance(
        positions,
        rules_data,
        price_by_symbol,
        transactions_by_symbol=txn_by_symbol,
    )

    # what-if: project the post-rebalance portfolio using per-symbol
    # historical CAGR blended at target weights
    years = body.years if body else 10
    blended = _blended_return(result.suggestions)
    projected = None
    if blended is not None and result.total_value > 0:
        from app.engine import projection

        cfg = projection.ProjectionConfig(
            start_value=result.total_value,
            monthly_contribution=0.0,
            expected_return=blended,
            years=years,
            inflation=0.0,
        )
        projected = projection.project(cfg)[-1]["value"]

    return {
        "total_value": round(result.total_value, 2),
        "suggestions": [asdict(s) for s in result.suggestions],
        "tax_total_est": round(result.tax_total_est, 2),
        "untaxed_symbols": result.untaxed_symbols,
        "drift_band_pct": rules_data.drift_band_pct,
        "projection": {
            "years": years,
            "blended_return_pct": round(blended * 100, 2) if blended is not None else None,
            "projected_value": round(projected, 2) if projected is not None else None,
        },
    }


def _blended_return(suggestions: list) -> float | None:
    """Blend per-symbol historical CAGR at post-rebalance target weights."""
    target_weights: dict[str, float] = {}
    for s in suggestions:
        target_pct = s["target_pct"] if isinstance(s, dict) else s.target_pct
        if target_pct > 0:
            target_weights[s["symbol"] if isinstance(s, dict) else s.symbol] = target_pct
    total = sum(target_weights.values())
    if total <= 0:
        return None
    rets = []
    for symbol, pct in target_weights.items():
        cagr = _symbol_cagr(symbol)
        if cagr is not None:
            rets.append(cagr * pct / total)
    if not rets:
        return None
    return sum(rets)


@router.get("/{portfolio_id}/transactions", response_model=list[TransactionRead])
def list_transactions(portfolio_id: int, db: Session = Depends(get_db)) -> list[models.Transaction]:
    _get_portfolio_or_404(portfolio_id, db)
    return (
        db.query(models.Transaction)
        .filter_by(portfolio_id=portfolio_id)
        .order_by(models.Transaction.trade_date, models.Transaction.id)
        .all()
    )


def _resolve_price(symbol: str, day: date) -> float | None:
    """Close on the exact date, else the nearest previous trading day.

    Looks in ohlcv first (stocks, ETFs, MF NAVs), then indices."""
    price = market_db.fetch_close_on(symbol, day)
    if price is not None:
        return price
    with market_db.get_connection() as con:
        row = con.execute(
            "SELECT close FROM indices WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            [symbol, day],
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


@router.post("/{portfolio_id}/transactions", response_model=TransactionRead, status_code=201)
def add_transaction(
    portfolio_id: int, body: TransactionCreate, db: Session = Depends(get_db)
) -> models.Transaction:
    _get_portfolio_or_404(portfolio_id, db)
    price = body.price
    if price == 0:
        price = _resolve_price(body.symbol.upper(), body.trade_date) or 0.0
    txn = models.Transaction(
        portfolio_id=portfolio_id,
        trade_date=body.trade_date,
        symbol=body.symbol.upper(),
        isin=body.isin,
        side=body.side,
        quantity=body.quantity,
        price=price,
        fees=body.fees,
        note=body.note,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


_CSV_ALIASES: dict[str, set[str]] = {
    "trade_date": {"date", "trade_date", "trade date", "transaction_date"},
    "symbol": {"symbol", "ticker", "scrip", "instrument"},
    "side": {"side", "action", "transaction_type", "transaction type"},
    "quantity": {"quantity", "qty", "shares"},
    "price": {"price", "rate", "trade_price", "trade price"},
    "fees": {"fees", "fee", "charges", "brokerage", "taxes"},
    "note": {"note", "notes", "comment", "remarks"},
}

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%b-%y")


def _parse_csv_date(raw: str) -> date | None:
    value = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_csv_side(raw: str) -> str | None:
    value = raw.strip().lower()
    if value.startswith("b"):
        return "buy"
    if value.startswith("s"):
        return "sell"
    if value in ("buy", "sell"):
        return value
    return None


def _map_csv_headers(fieldnames: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for header in fieldnames or []:
        normalized = header.strip().lower().replace(" ", "_")
        for canonical, aliases in _CSV_ALIASES.items():
            if normalized in aliases or normalized == canonical:
                mapping[header] = canonical
                break
    return mapping


@router.post("/{portfolio_id}/transactions/import")
async def import_transactions_csv(
    portfolio_id: int, file: UploadFile, db: Session = Depends(get_db)
) -> dict:
    """Import transactions from a CSV, deduplicating exact repeats.

    Expected columns (flexible aliases): date, symbol, side, quantity, price,
    fees, note. Rows with price 0 get the close from the trade date (nearest
    previous trading day); rows without any price data are skipped."""
    _get_portfolio_or_404(portfolio_id, db)
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    by_canonical = {
        canonical: header
        for header, canonical in _map_csv_headers(reader.fieldnames).items()
    }
    if not {"trade_date", "symbol", "quantity"}.issubset(by_canonical):
        raise HTTPException(
            status_code=422,
            detail=f"CSV must have date, symbol and quantity columns; got: {reader.fieldnames}",
        )

    existing = {
        (t.trade_date, t.symbol, t.side, t.quantity, t.price)
        for t in db.query(models.Transaction).filter_by(portfolio_id=portfolio_id).all()
    }

    imported = 0
    skipped: list[dict] = []
    seen_in_file: set[tuple] = set()
    new_txns: list[models.Transaction] = []

    for row_number, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        raw_date = (row.get(by_canonical["trade_date"]) or "").strip()
        symbol = (row.get(by_canonical["symbol"]) or "").strip().upper()
        raw_qty = (row.get(by_canonical["quantity"]) or "").strip().replace(",", "")
        raw_side = (row.get(by_canonical["side"]) or "").strip() if "side" in by_canonical else ""
        raw_price = (row.get(by_canonical["price"]) or "").strip().replace(",", "") if "price" in by_canonical else ""
        raw_fees = (row.get(by_canonical["fees"]) or "").strip().replace(",", "") if "fees" in by_canonical else ""
        note = (row.get(by_canonical["note"]) or "").strip() or None if "note" in by_canonical else None

        def skip(reason: str) -> None:
            skipped.append({"row": row_number, "reason": reason})

        trade_date = _parse_csv_date(raw_date)
        if trade_date is None:
            skip(f"unparseable date: {raw_date!r}")
            continue
        if not symbol:
            skip("missing symbol")
            continue
        side = _parse_csv_side(raw_side)
        if side is None:
            skip(f"unparseable side: {raw_side!r}")
            continue
        try:
            quantity = float(raw_qty)
        except ValueError:
            skip(f"unparseable quantity: {raw_qty!r}")
            continue
        if quantity <= 0:
            skip(f"non-positive quantity: {quantity}")
            continue
        try:
            price = float(raw_price) if raw_price else 0.0
        except ValueError:
            skip(f"unparseable price: {raw_price!r}")
            continue
        if price < 0:
            skip(f"negative price: {price}")
            continue
        try:
            fees = float(raw_fees) if raw_fees else 0.0
        except ValueError:
            fees = 0.0

        resolved = price
        if resolved == 0:
            resolved = _resolve_price(symbol, trade_date) or 0.0
            if resolved == 0:
                skip(f"no price data for {symbol} on/before {trade_date.isoformat()}")
                continue

        key = (trade_date, symbol, side, quantity, resolved)
        if key in existing or key in seen_in_file:
            skip("duplicate of existing transaction")
            continue
        seen_in_file.add(key)
        new_txns.append(
            models.Transaction(
                portfolio_id=portfolio_id,
                trade_date=trade_date,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=resolved,
                fees=fees,
                note=note,
            )
        )
        existing.add(key)
        imported += 1

    if new_txns:
        db.add_all(new_txns)
        db.commit()
    return {"imported": imported, "total": imported + len(skipped), "skipped": skipped}


@router.delete("/{portfolio_id}/transactions/{txn_id}", status_code=204)
def delete_transaction(
    portfolio_id: int, txn_id: int, db: Session = Depends(get_db)
) -> None:
    _get_portfolio_or_404(portfolio_id, db)
    txn = db.get(models.Transaction, txn_id)
    if txn is None or txn.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="transaction not found")
    db.delete(txn)
    db.commit()


@router.get("/{portfolio_id}/dividends", response_model=list[DividendRead])
def list_dividends(portfolio_id: int, db: Session = Depends(get_db)) -> list[models.Dividend]:
    _get_portfolio_or_404(portfolio_id, db)
    return (
        db.query(models.Dividend)
        .filter_by(portfolio_id=portfolio_id)
        .order_by(models.Dividend.ex_date, models.Dividend.id)
        .all()
    )


@router.post("/{portfolio_id}/dividends", response_model=DividendRead, status_code=201)
def add_dividend(
    portfolio_id: int, body: DividendCreate, db: Session = Depends(get_db)
) -> models.Dividend:
    _get_portfolio_or_404(portfolio_id, db)
    dividend = models.Dividend(
        portfolio_id=portfolio_id,
        ex_date=body.ex_date,
        symbol=body.symbol.upper(),
        isin=body.isin,
        amount_per_share=body.amount_per_share,
        total_amount=body.total_amount,
        note=body.note,
    )
    db.add(dividend)
    db.commit()
    db.refresh(dividend)
    return dividend


@router.delete("/{portfolio_id}/dividends/{dividend_id}", status_code=204)
def delete_dividend(
    portfolio_id: int, dividend_id: int, db: Session = Depends(get_db)
) -> None:
    _get_portfolio_or_404(portfolio_id, db)
    dividend = db.get(models.Dividend, dividend_id)
    if dividend is None or dividend.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="dividend not found")
    db.delete(dividend)
    db.commit()


@watchlist_router.get("", response_model=list[WatchlistItemRead])
def list_watchlist(db: Session = Depends(get_db)) -> list[models.WatchlistItem]:
    return db.query(models.WatchlistItem).order_by(models.WatchlistItem.symbol).all()


@watchlist_router.post("", response_model=WatchlistItemRead, status_code=201)
def add_watchlist_item(
    body: WatchlistItemCreate, db: Session = Depends(get_db)
) -> models.WatchlistItem:
    item = models.WatchlistItem(symbol=body.symbol.upper(), note=body.note)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@watchlist_router.delete("/{item_id}", status_code=204)
def delete_watchlist_item(item_id: int, db: Session = Depends(get_db)) -> None:
    item = db.get(models.WatchlistItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="watchlist item not found")
    db.delete(item)
    db.commit()
