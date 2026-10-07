from collections.abc import Iterator
from datetime import date, datetime, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.engine import db as market_db
from app.main import app


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _seed_market_data(symbol: str = "AAPL", days: int = 60) -> None:
    base = date.today() - timedelta(days=days)
    market_db.merge_ohlcv(
        pl.DataFrame(
            {
                "symbol": [symbol] * days,
                "ts": [base + timedelta(days=i) for i in range(days)],
                "open": [100.0 + i for i in range(days)],
                "high": [101.0 + i for i in range(days)],
                "low": [99.0 + i for i in range(days)],
                "close": [100.5 + i for i in range(days)],
                "volume": [1_000_000 + i for i in range(days)],
            }
        )
    )


def test_health(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_ohlcv_endpoint(client: TestClient) -> None:
    _seed_market_data()
    res = client.get("/api/market/ohlcv/AAPL")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 60
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["close"] == 100.5


def _seed_financials(symbol: str = "FAKE") -> None:
    """Two annual periods + four quarters with clean, predictable ratios."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import FinancialStatementRow

    rows: list[dict] = []
    for period_end, revenue, ni, ebit in [
        (date(2024, 3, 31), 800.0, 80.0, 120.0),
        (date(2025, 3, 31), 1000.0, 100.0, 150.0),
    ]:
        rows += [
            {"symbol": symbol, "period_type": "annual", "period_end": period_end, "statement": "income", "line_item": "TotalRevenue", "value": revenue},
            {"symbol": symbol, "period_type": "annual", "period_end": period_end, "statement": "income", "line_item": "NetIncome", "value": ni},
            {"symbol": symbol, "period_type": "annual", "period_end": period_end, "statement": "income", "line_item": "EBIT", "value": ebit},
        ]
    for period_end in [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)]:
        rows += [
            {"symbol": symbol, "period_type": "quarterly", "period_end": period_end, "statement": "income", "line_item": "TotalRevenue", "value": 250.0},
            {"symbol": symbol, "period_type": "quarterly", "period_end": period_end, "statement": "income", "line_item": "NetIncome", "value": 25.0},
            {"symbol": symbol, "period_type": "quarterly", "period_end": period_end, "statement": "income", "line_item": "EBIT", "value": 37.5},
        ]
    rows += [
        {"symbol": symbol, "period_type": "annual", "period_end": date(2025, 3, 31), "statement": "balance", "line_item": "TotalAssets", "value": 1000.0},
        {"symbol": symbol, "period_type": "annual", "period_end": date(2025, 3, 31), "statement": "balance", "line_item": "CurrentLiabilities", "value": 200.0},
        {"symbol": symbol, "period_type": "annual", "period_end": date(2025, 3, 31), "statement": "balance", "line_item": "TotalDebt", "value": 250.0},
        {"symbol": symbol, "period_type": "annual", "period_end": date(2025, 3, 31), "statement": "balance", "line_item": "OrdinarySharesNumber", "value": 100.0},
        {"symbol": symbol, "period_type": "annual", "period_end": date(2025, 3, 31), "statement": "balance", "line_item": "CommonStockEquity", "value": 500.0},
    ]
    with SessionLocal() as session:
        for row in rows:
            session.execute(sqlite_insert(FinancialStatementRow).values(**row).on_conflict_do_nothing())
        session.commit()


def _sync_one_security(symbol: str = "FAKE") -> None:
    from app.ingest.universe import sync_securities
    from app.state.db import SessionLocal
    from app.state.models import SecurityInfo
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    sync_securities(
        pl.DataFrame(
            {
                "symbol": [symbol],
                "name": [f"{symbol} Ltd"],
                "series": ["EQ"],
                "isin": [None],
                "instrument_type": ["stock"],
                "listed_on": [None],
            }
        )
    )

    # Also insert into securities_info for shares_outstanding
    with SessionLocal() as session:
        session.execute(
            sqlite_insert(SecurityInfo).values(
                symbol=symbol,
                shares_outstanding=100.0,
            ).on_conflict_do_update(
                index_elements=[SecurityInfo.symbol],
                set_={"shares_outstanding": 100.0},
            )
        )
        session.commit()


def test_screener_endpoint(client: TestClient) -> None:
    from app.api.routes import market as market_routes
    from app.engine import db as market_db

    _seed_market_data("MSFT", days=30)
    _seed_market_data("FAKE", days=30)
    _sync_one_security("FAKE")
    _seed_financials("FAKE")
    market_routes._screener_cache = None

    res = client.post(
        "/api/market/screener",
        json={"filters": [{"metric": "pe", "op": "lt", "value": 200}], "rank_by": "pe"},
    )
    assert res.status_code == 200
    body = res.json()
    symbols = [r["symbol"] for r in body["rows"]]
    assert "FAKE" in symbols
    assert "MSFT" not in symbols  # no financials -> null PE -> filtered
    fake = next(r for r in body["rows"] if r["symbol"] == "FAKE")
    assert fake["pe"] == 129.5  # 100 shares x 129.5 close / 100 TTM NI
    assert fake["name"] == "FAKE Ltd"

    res = client.post("/api/market/screener", json={"filters": [{"metric": "nope", "op": "lt", "value": 1}]})
    assert res.status_code == 422

    res = client.post("/api/market/screener", json={"instrument_types": ["etf"]})
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_fundamentals_endpoints(client: TestClient) -> None:
    _seed_market_data("FAKE", days=30)
    _sync_one_security("FAKE")
    _seed_financials("FAKE")
    
    res = client.get("/api/market/fundamentals/FAKE")
    assert res.status_code == 200
    body = res.json()
    assert body["latest"]["pe"]["value"] == 129.5  # 100 shares x 129.5 close / 100 TTM NI
    assert body["latest"]["pb"]["value"] == 25.9
    assert body["latest"]["roe"]["value"] == 0.2
    assert body["latest"]["roce"]["value"] == 0.1875
    assert body["latest"]["debt_to_equity"]["value"] == 0.5
    assert body["latest"]["net_margin"]["value"] == 0.1
    assert body["latest"]["revenue_growth"]["value"] == 0.25
    assert len(body["history"]) == 6

    res = client.get("/api/market/fundamentals/FAKE/statements?statement=profit_loss")
    assert res.status_code == 200
    assert "profit_loss" in res.json()["statements"]

    res = client.get("/api/market/fundamentals/UNKNOWN_SYM")
    assert res.status_code == 200
    assert res.json()["latest"] is None


def test_screens_crud(client: TestClient) -> None:
    res = client.post(
        "/api/screens",
        json={
            "name": "cheap-quality",
            "filters": [
                {"metric": "pe", "op": "lt", "value": 20},
                {"metric": "roe", "op": "gt", "value": 0.15},
            ],
            "rank_by": "roe",
        },
    )
    assert res.status_code == 201
    screen = res.json()
    assert len(screen["filters"]) == 2

    res = client.get("/api/screens")
    assert res.status_code == 200
    assert any(s["name"] == "cheap-quality" for s in res.json())

    res = client.delete(f"/api/screens/{screen['id']}")
    assert res.status_code == 204


def test_backtest_endpoint(client: TestClient) -> None:
    res = client.post("/api/market/backtest", json={"symbol": "AAPL", "fast": 5, "slow": 20})
    assert res.status_code == 200
    body = res.json()
    assert body["symbol"] == "AAPL"
    assert body["final_equity"] > 0


def test_portfolio_crud(client: TestClient) -> None:
    res = client.post("/api/portfolios", json={"name": "main"})
    assert res.status_code == 201
    portfolio = res.json()
    assert portfolio["name"] == "main"
    assert portfolio["base_currency"] == "INR"
    assert "positions" not in portfolio

    res = client.get("/api/portfolios")
    assert res.status_code == 200
    assert any(p["name"] == "main" for p in res.json())

    res = client.post("/api/portfolios", json={"name": "main"})
    assert res.status_code == 409

    res = client.delete(f"/api/portfolios/{portfolio['id']}")
    assert res.status_code == 204


def test_transaction_crud_and_computed_positions(client: TestClient) -> None:
    res = client.post("/api/portfolios", json={"name": "txn"})
    assert res.status_code == 201
    pid = res.json()["id"]

    res = client.post(
        f"/api/portfolios/{pid}/transactions",
        json={
            "trade_date": "2026-01-05",
            "symbol": "reliance",
            "side": "buy",
            "quantity": 10,
            "price": 100.0,
            "fees": 50.0,
        },
    )
    assert res.status_code == 201
    txn = res.json()
    assert txn["symbol"] == "RELIANCE"
    assert txn["portfolio_id"] == pid

    client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": "2026-02-05", "symbol": "RELIANCE", "side": "buy", "quantity": 10, "price": 200.0},
    )
    client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": "2026-03-05", "symbol": "RELIANCE", "side": "sell", "quantity": 10, "price": 250.0},
    )

    res = client.get(f"/api/portfolios/{pid}/positions")
    assert res.status_code == 200
    positions = res.json()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["quantity"] == 10.0
    # (10*100 + 50 fees + 10*200) / 20 shares = 152.5; the sell keeps avg cost
    assert pos["avg_cost"] == 152.5
    assert pos["invested"] == 1525.0

    res = client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": "2026-01-05", "symbol": "X", "side": "short", "quantity": 1, "price": 1},
    )
    assert res.status_code == 422

    res = client.delete(f"/api/portfolios/{pid}/transactions/{txn['id']}")
    assert res.status_code == 204


def test_dividend_crud(client: TestClient) -> None:
    res = client.post("/api/portfolios", json={"name": "div"})
    pid = res.json()["id"]

    res = client.post(
        f"/api/portfolios/{pid}/dividends",
        json={"ex_date": "2026-01-10", "symbol": "tcs", "amount_per_share": 10.0, "total_amount": 100.0},
    )
    assert res.status_code == 201
    dividend = res.json()
    assert dividend["symbol"] == "TCS"
    assert dividend["total_amount"] == 100.0

    res = client.get(f"/api/portfolios/{pid}/dividends")
    assert res.status_code == 200
    assert any(d["symbol"] == "TCS" for d in res.json())

    res = client.delete(f"/api/portfolios/{pid}/dividends/{dividend['id']}")
    assert res.status_code == 204


def test_indices_endpoint(client: TestClient) -> None:
    from app.engine import db as market_db

    market_db.upsert_indices(
        pl.DataFrame(
            {
                "symbol": ["NIFTY_50"] * 5,
                "ts": [datetime(2026, 1, 1 + i) for i in range(5)],
                "close": [100.0 + i for i in range(5)],
            }
        )
    )
    res = client.get("/api/market/indices/NIFTY_50")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 5
    assert rows[0]["close"] == 100.0


def test_index_summary_separates_live_and_usable(client: TestClient) -> None:
    """Freshness alone is not enough: a symbol Yahoo serves a single bar for must
    not be offered as a chart or benchmark until it has real history."""
    from app.engine import db as market_db

    base = datetime(2026, 1, 1)
    fresh_full = [
        {"symbol": "TST_LIVE_FULL", "ts": base + timedelta(days=i), "close": 100.0 + i}
        for i in range(40)
    ]
    fresh_stub = [{"symbol": "TST_LIVE_STUB", "ts": base + timedelta(days=39), "close": 5.0}]
    retired = [
        {"symbol": "TST_RETIRED", "ts": datetime(2015, 1, 1) + timedelta(days=i), "close": 9.0}
        for i in range(400)
    ]
    market_db.upsert_indices(pl.DataFrame(fresh_full + fresh_stub + retired))

    res = client.get("/api/market/indices")
    assert res.status_code == 200
    by_symbol = {r["symbol"]: r for r in res.json()}

    assert by_symbol["TST_LIVE_FULL"]["is_live"] is True
    assert by_symbol["TST_LIVE_FULL"]["is_usable"] is True
    assert by_symbol["TST_LIVE_FULL"]["rows"] == 40

    # fresh, but a one-bar stub -> live yet not usable
    assert by_symbol["TST_LIVE_STUB"]["is_live"] is True
    assert by_symbol["TST_LIVE_STUB"]["is_usable"] is False

    # plenty of history, but stopped being updated -> history only
    assert by_symbol["TST_RETIRED"]["is_live"] is False
    assert by_symbol["TST_RETIRED"]["is_usable"] is False
    assert by_symbol["TST_RETIRED"]["rows"] == 400


def test_watchlist_crud(client: TestClient) -> None:
    res = client.post("/api/watchlist", json={"symbol": "aapl", "note": "watch earnings"})
    assert res.status_code == 201
    item = res.json()
    assert item["symbol"] == "AAPL"

    res = client.get("/api/watchlist")
    assert res.status_code == 200
    assert any(i["symbol"] == "AAPL" for i in res.json())

    res = client.delete(f"/api/watchlist/{item['id']}")
    assert res.status_code == 204


def test_websocket_echo(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "echo: ping"


def test_ohlcv_adjusted_endpoint(client: TestClient) -> None:
    from app.state.db import SessionLocal
    from app.state.models import CorporateAction

    _seed_market_data("ADJ", days=60)
    with SessionLocal() as session:
        session.add(
            CorporateAction(
                symbol="ADJ",
                ex_date=date.today() - timedelta(days=30),
                action_type="split",
                factor=2.0,
                purpose="FACE VALUE SPLIT FROM RS.10/- TO RS.5/-",
            )
        )
        session.commit()

    raw = client.get("/api/market/ohlcv/ADJ").json()
    adj = client.get("/api/market/ohlcv/ADJ?adjusted=true").json()
    assert len(raw) == len(adj) == 60
    assert raw[0]["close"] == 100.5
    assert adj[0]["close"] == 50.25
    assert adj[-1]["close"] == raw[-1]["close"]


def test_xirr(client: TestClient) -> None:
    from datetime import date

    from app.engine.wealth import xirr

    # Buy 100 at 100 on day 0, worth 110 one year later -> ~10%
    flows = [(date(2025, 1, 1), -10_000.0), (date(2026, 1, 1), 11_000.0)]
    rate = xirr(flows)
    assert rate is not None
    assert abs(rate - 0.10) < 0.001

    # SIP: 1200/yr for 2 years, worth 2600 at end of year 2
    flows = [
        (date(2024, 1, 1), -1200.0),
        (date(2025, 1, 1), -1200.0),
        (date(2026, 1, 1), 2600.0),
    ]
    rate = xirr(flows)
    assert rate is not None and rate > 0

    assert xirr([(date(2025, 1, 1), -100.0)]) is None
    assert xirr([(date(2025, 1, 1), -100.0), (date(2025, 2, 1), -100.0)]) is None


def test_fifo_lots(client: TestClient) -> None:
    from datetime import date

    from app.engine.wealth import fifo_lots

    class T:
        def __init__(self, trade_date, symbol, side, quantity, price, fees=0.0):
            self.trade_date = trade_date
            self.symbol = symbol
            self.side = side
            self.quantity = quantity
            self.price = price
            self.fees = fees
            self.id = 0

    txns = [
        T(date(2024, 1, 1), "X", "buy", 100, 100.0),
        T(date(2025, 6, 1), "X", "buy", 100, 200.0),
        T(date(2025, 7, 1), "X", "sell", 150, 250.0),  # consumes 100 LT + 50 ST
    ]
    result = fifo_lots(txns)
    assert len(result.lots) == 2
    first, second = result.lots
    assert first.term == "lt" and first.quantity == 100
    assert first.gain == (250.0 - 100.0) * 100
    assert second.term == "st" and second.quantity == 50
    assert second.gain == (250.0 - 200.0) * 50
    assert result.ltcg == 15_000.0
    assert result.stcg == 2_500.0
    # LTCG tax: (15000 - 125000) clamped to 0
    assert result.ltcg_tax == 0.0
    assert result.stcg_tax == 500.0

    big = fifo_lots([T(date(2024, 1, 1), "X", "buy", 10000, 100.0), T(date(2025, 7, 1), "X", "sell", 10000, 300.0)])
    assert big.ltcg == 2_000_000.0
    assert big.ltcg_tax == (2_000_000.0 - 125_000.0) * 0.125


def test_summary_and_gains_endpoints(client: TestClient) -> None:
    res = client.post("/api/portfolios", json={"name": "sum"})
    pid = res.json()["id"]

    today = date.today()
    buys = [
        {"trade_date": (today - timedelta(days=400)).isoformat(), "symbol": "FAKE", "side": "buy", "quantity": 10, "price": 100.0},
        {"trade_date": (today - timedelta(days=30)).isoformat(), "symbol": "FAKE", "side": "buy", "quantity": 10, "price": 200.0},
    ]
    for b in buys:
        res = client.post(f"/api/portfolios/{pid}/transactions", json=b)
        assert res.status_code == 201
    # sell at today's-ish price (159.5) -> realized on 10 shares
    res = client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": (today - timedelta(days=10)).isoformat(), "symbol": "FAKE", "side": "sell", "quantity": 10, "price": 159.5},
    )
    assert res.status_code == 201

    client.post(
        f"/api/portfolios/{pid}/dividends",
        json={"ex_date": (today - timedelta(days=20)).isoformat(), "symbol": "FAKE", "amount_per_share": 5.0},
    )

    res = client.get(f"/api/portfolios/{pid}/summary")
    assert res.status_code == 200
    body = res.json()
    assert len(body["holdings"]) == 1
    holding = body["holdings"][0]
    # 10 shares held (10 + 10 - 10); FAKE was seeded with days=30 in
    # test_screener_endpoint, so the latest close is 129.5
    assert holding["quantity"] == 10.0
    assert holding["close"] == 129.5
    assert holding["current_value"] == 1295.0
    # avg cost (10*100 + 10*200)/20 = 150 -> invested 1500, unrealized -205
    assert holding["invested"] == 1500.0
    assert abs(holding["unrealized"] - (-205.0)) < 0.01
    # realized avg-cost: sold at 159.5, avg cost 150 -> +95
    assert abs(holding["realized"] - 95.0) < 0.01
    assert body["dividends"] == 100.0  # 5/share x 20 held at ex-date
    assert body["xirr"] is not None

    res = client.get(f"/api/portfolios/{pid}/gains")
    assert res.status_code == 200
    gains = res.json()
    assert len(gains["lots"]) == 1
    lot = gains["lots"][0]
    assert lot["term"] == "lt"  # FIFO consumes the 400-day-old lot first
    assert lot["gain"] == 595.0  # (159.5 - 100) x 10
    assert gains["ltcg"] == 595.0
    assert gains["stcg"] == 0.0

    res = client.get(f"/api/portfolios/{pid}/equity-curve")
    assert res.status_code == 200
    curve = res.json()
    assert len(curve) > 0
    assert curve[-1]["value"] > 0


def test_projection_engine() -> None:
    from app.engine.projection import ProjectionConfig, monte_carlo, project, required_monthly_contribution

    # Manual check: 0 start, 100/mo, 12% annual (geometric monthly ~0.9489%)
    cfg = ProjectionConfig(start_value=0, monthly_contribution=100, expected_return=0.12, years=2, inflation=0.0)
    rows = project(cfg)
    assert len(rows) == 24
    assert rows[0]["value"] == 100.0
    r = (1.12) ** (1 / 12) - 1
    expected_m2 = 100 * (1 + r) + 100
    assert abs(rows[1]["value"] - expected_m2) < 0.01
    # invested accumulates contributions
    assert rows[-1]["invested"] == 2400.0

    # step-up: contribution grows 10% after month 12
    cfg2 = ProjectionConfig(start_value=0, monthly_contribution=100, annual_step_up_pct=0.10, expected_return=0.0, years=3, inflation=0.0)
    rows2 = project(cfg2)
    assert rows2[11]["value"] == 1200.0  # 12 months x 100
    assert rows2[12]["value"] == 1310.0  # month 13: 1200 carried + 110 contribution

    # MC: seeded -> median path matches the deterministic projection
    mc = monte_carlo(cfg, num_paths=2000, seed=42)
    assert mc["paths"] == 2000
    p50 = mc["final_percentiles"]["p50"]
    det = rows[-1]["value"]
    assert abs(p50 - det) / det < 0.05  # median within 5% of deterministic

    # goal: required SIP to reach 10L in 5 years at 12%
    req = required_monthly_contribution(1_000_000, ProjectionConfig(years=5, expected_return=0.12))
    assert req is not None and req > 0
    final = project(ProjectionConfig(years=5, expected_return=0.12, monthly_contribution=req))[-1]["value"]
    assert abs(final - 1_000_000) < 100


def test_scenario_endpoints(client: TestClient) -> None:
    res = client.post(
        "/api/scenarios",
        json={
            "name": "fi-25y",
            "params": {
                "monthly_contribution": 25000,
                "expected_return": 0.12,
                "years": 25,
                "annual_step_up_pct": 0.08,
                "inflation": 0.05,
                "assets": [
                    {"name": "equity", "weight": 0.7, "expected_return": 0.14, "volatility": 0.16},
                    {"name": "debt", "weight": 0.3, "expected_return": 0.07, "volatility": 0.03},
                ],
            },
        },
    )
    assert res.status_code == 201
    scenario = res.json()
    assert scenario["params"]["monthly_contribution"] == 25000
    assert len(scenario["params"]["assets"]) == 2

    res = client.post("/api/scenarios", json={"name": "fi-25y"})
    assert res.status_code == 409

    res = client.post(f"/api/scenarios/{scenario['id']}/run?num_paths=500")
    assert res.status_code == 200
    body = res.json()
    assert len(body["projection"]) == 300
    assert body["monte_carlo"]["paths"] == 500
    assert "p10" in body["monte_carlo"]["final_percentiles"]
    assert len(body["monte_carlo"]["bands"]) == 300
    # blended return: 0.7*0.14 + 0.3*0.07 = 0.119
    assert abs(body["monte_carlo"]["expected_return"] - 0.119) < 0.001

    res = client.post(
        "/api/scenarios/preview",
        json={"params": {"monthly_contribution": 5000, "years": 10}, "num_paths": 200},
    )
    assert res.status_code == 200
    assert len(res.json()["projection"]) == 120

    res = client.post(
        "/api/scenarios/goal",
        json={"target": 1_000_000, "years": 5, "expected_return": 0.12},
    )
    assert res.status_code == 200
    goal = res.json()
    assert goal["required_monthly_contribution"] > 0
    assert abs(goal["projected_value"] - 1_000_000) < 100

    res = client.put(
        f"/api/scenarios/{scenario['id']}",
        json={"name": "fi-25y", "params": {"monthly_contribution": 30000, "years": 20}},
    )
    assert res.status_code == 200
    assert res.json()["params"]["monthly_contribution"] == 30000

    res = client.delete(f"/api/scenarios/{scenario['id']}")
    assert res.status_code == 204


def test_allocation_engine() -> None:
    from app.engine.allocation import AllocationConfig, monthly_close_matrix, performance_metrics, simulate_allocation

    # 12 months, 2 symbols: A returns +10%/mo, B flat
    months = [date(2025 + (i // 12), (i % 12) + 1, 1) for i in range(12)]
    prices = pl.DataFrame(
        {
            "month": months,
            "A": [100.0 * (1.1**i) for i in range(12)],
            "B": [100.0] * 12,
        }
    ).with_columns(pl.lit(None, dtype=pl.Datetime).alias("ts"))

    # monthly_close_matrix expects symbol/ts/close long format — build directly
    long = pl.DataFrame(
        {
            "symbol": ["A"] * 12 + ["B"] * 12,
            "ts": months + months,
            "close": [100.0 * (1.1**i) for i in range(12)] + [100.0] * 12,
        }
    )
    matrix = monthly_close_matrix(long)
    assert matrix.height == 12
    assert "A" in matrix.columns and "B" in matrix.columns

    cfg = AllocationConfig(symbols=["A", "B"], weights=[0.5, 0.5], initial_capital=10_000.0)
    cashflows: list[tuple[date, float]] = []
    result = simulate_allocation(matrix, cfg, cashflows=cashflows)
    curve = result["curve"]
    assert len(curve) == 12
    # initial deployed at month 1: 5000 each; A grows 10%/mo, B flat
    assert curve[0]["value"] == 10_000.0
    assert curve[0]["invested"] == 10_000.0
    expected_m2 = 5000 * 1.1 + 5000
    assert abs(curve[1]["value"] - expected_m2) < 0.01
    final = curve[-1]["value"]
    # deployed at the month-1 close, so A compounds for 11 more months
    expected_final = 5000 * (1.1**11) + 5000
    assert abs(final - expected_final) < 0.01
    assert len(cashflows) == 2  # initial + final value

    # monthly contribution flows in per weights
    cfg2 = AllocationConfig(
        symbols=["A", "B"], weights=[0.5, 0.5], initial_capital=0.0, monthly_contribution=1000.0
    )
    flows2: list[tuple[date, float]] = []
    result2 = simulate_allocation(matrix, cfg2, cashflows=flows2)
    assert result2["curve"][-1]["invested"] == 12_000.0
    assert len(flows2) == 13  # 12 contributions + final value

    # rebalancing yearly keeps weights pinned
    cfg3 = AllocationConfig(
        symbols=["A", "B"], weights=[0.5, 0.5], initial_capital=10_000.0, rebalance_freq="yearly"
    )
    result3 = simulate_allocation(matrix, cfg3, cashflows=None)
    final3 = result3["curve"][-1]["value"]
    # after month-12 rebalance: total split 50/50 — same as expected_final here
    assert abs(final3 - expected_final) < 0.01

    metrics = performance_metrics(curve, cashflows)
    assert metrics["total_return_pct"] > 0
    assert metrics["xirr_pct"] is not None
    assert metrics["max_drawdown_pct"] >= 0


def test_backtest_allocation_endpoint(client: TestClient) -> None:
    today = date.today()
    base = today - timedelta(days=400)
    days = [base + timedelta(days=i) for i in range(400)]

    def seed(sym: str, growth: float) -> None:
        market_db.merge_ohlcv(
            pl.DataFrame(
                {
                    "symbol": [sym] * 400,
                    "ts": days,
                    "open": [100.0 * growth**i for i in range(400)],
                    "high": [101.0 * growth**i for i in range(400)],
                    "low": [99.0 * growth**i for i in range(400)],
                    "close": [100.0 * growth**i for i in range(400)],
                    "volume": [1_000_000] * 400,
                }
            )
        )

    seed("ALPHA", 1.01)
    seed("BETA", 1.005)
    market_db.upsert_indices(
        pl.DataFrame(
            {
                "symbol": ["NIFTY_50"] * 400,
                "ts": days,
                "close": [100.0 * (1.0008**i) for i in range(400)],
            }
        )
    )

    res = client.post(
        "/api/market/backtest-allocation",
        json={
            "symbols": ["alpha", "beta"],
            "weights": [0.6, 0.4],
            "initial_capital": 100_000.0,
            "monthly_contribution": 5000.0,
            "benchmark": "NIFTY_50",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["symbols"] == ["ALPHA", "BETA"]
    assert body["missing_symbols"] == []
    assert len(body["strategy"]["curve"]) > 12
    m = body["strategy"]["metrics"]
    assert m["final_value"] > m["total_invested"]
    assert m["xirr_pct"] is not None and m["xirr_pct"] > 0
    assert body["benchmark"] is not None
    # strategy outperformed the slow benchmark
    assert m["final_value"] > body["benchmark"]["metrics"]["final_value"]

    # unknown symbols -> 422
    res = client.post(
        "/api/market/backtest-allocation",
        json={"symbols": ["NOSUCH1", "NOSUCH2"]},
    )
    assert res.status_code == 422


def test_rebalance_engine() -> None:
    from datetime import date as d

    from app.engine.rebalance import RebalanceRules, RebalanceTarget, suggest_rebalance

    positions = [
        {"symbol": "A", "quantity": 100, "avg_cost": 100.0},
        {"symbol": "B", "quantity": 100, "avg_cost": 100.0},
    ]
    price_by_symbol = {"A": 200.0, "B": 100.0}
    rules = RebalanceRules(
        targets=[RebalanceTarget(symbol="A", target_pct=50.0), RebalanceTarget(symbol="B", target_pct=50.0)],
        drift_band_pct=5.0,
    )
    result = suggest_rebalance(positions, rules, price_by_symbol)
    # total 30000: A = 20000 (66.7%), B = 10000 (33.3%); target 50/50
    assert result.total_value == 30_000.0
    a = next(s for s in result.suggestions if s.symbol == "A")
    b = next(s for s in result.suggestions if s.symbol == "B")
    assert a.action == "sell" and a.amount == 5000.0
    assert b.action == "buy" and b.amount == 5000.0  # underweight -> fresh purchase

    # no transaction data -> sell flagged as untaxed
    assert "A" in result.untaxed_symbols

    # with FIFO transaction data, long-term sale gets taxed at LTCG rate
    class T:
        def __init__(self, trade_date, symbol, side, quantity, price, fees=0.0):
            self.trade_date = trade_date
            self.symbol = symbol
            self.side = side
            self.quantity = quantity
            self.price = price
            self.fees = fees
            self.id = 0

    txns = {"A": [T(d(2023, 1, 1), "A", "buy", 100, 100.0)]}
    result2 = suggest_rebalance(positions, rules, price_by_symbol, transactions_by_symbol=txns)
    a2 = next(s for s in result2.suggestions if s.symbol == "A")
    # sell 5000 of value = 25 shares at 200; gain (200-100)*25 = 2500; LT -> 12.5%
    assert a2.tax_est == 2500.0 * 0.125
    assert "A" not in result2.untaxed_symbols

    # symbol not held but in targets -> buy suggestion
    rules3 = RebalanceRules(
        targets=[RebalanceTarget(symbol="C", target_pct=20.0), RebalanceTarget(symbol="A", target_pct=80.0)],
        drift_band_pct=5.0,
    )
    result3 = suggest_rebalance([{"symbol": "A", "quantity": 100, "avg_cost": 100.0}], rules3, price_by_symbol)
    c3 = next(s for s in result3.suggestions if s.symbol == "C")
    assert c3.action == "buy" and c3.amount == 4000.0  # 20% of 20000 (A only)
    # A at 100% vs 80% target = 20% drift -> sell 4000
    a3 = next(s for s in result3.suggestions if s.symbol == "A")
    assert a3.action == "sell" and a3.amount == 4000.0

    # inside the band -> hold
    rules4 = RebalanceRules(
        targets=[RebalanceTarget(symbol="A", target_pct=60.0), RebalanceTarget(symbol="B", target_pct=40.0)],
        drift_band_pct=10.0,
    )
    result4 = suggest_rebalance(positions, rules4, price_by_symbol)
    assert all(s.action == "hold" for s in result4.suggestions)


def test_rebalance_endpoints(client: TestClient) -> None:
    res = client.post("/api/portfolios", json={"name": "rebal"})
    pid = res.json()["id"]
    today = date.today()

    client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": (today - timedelta(days=500)).isoformat(), "symbol": "ALPHA", "side": "buy", "quantity": 100, "price": 100.0},
    )
    client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": (today - timedelta(days=400)).isoformat(), "symbol": "BETA", "side": "buy", "quantity": 100, "price": 100.0},
    )

    res = client.put(
        f"/api/portfolios/{pid}/rules",
        json={
            "targets": [
                {"symbol": "ALPHA", "target_pct": 50.0},
                {"symbol": "BETA", "target_pct": 50.0},
            ],
            "drift_band_pct": 5.0,
        },
    )
    assert res.status_code == 200
    assert res.json()["rebalancing_rules"]["drift_band_pct"] == 5.0

    res = client.post(f"/api/portfolios/{pid}/rebalance", json={"years": 10})
    assert res.status_code == 200
    body = res.json()
    assert body["total_value"] > 0
    assert body["drift_band_pct"] == 5.0
    # ALPHA (x1.01/day) is overweight vs BETA -> sell suggested
    alpha = next(s for s in body["suggestions"] if s["symbol"] == "ALPHA")
    assert alpha["action"] in ("sell", "hold")
    # tax estimate present for sells with FIFO lots
    sells = [s for s in body["suggestions"] if s["action"] == "sell"]
    for s in sells:
        assert s["tax_est"] >= 0
    assert body["projection"]["years"] == 10
    # projection may be None when price data is stale/short, otherwise > 0
    if body["projection"]["projected_value"] is not None:
        assert body["projection"]["projected_value"] > 0

    res = client.get(f"/api/portfolios/{pid}")
    assert res.status_code == 200
    assert res.json()["rebalancing_rules"]["targets"][0]["symbol"] == "ALPHA"


def _seed_ticker_world() -> None:
    """Securities master + OHLCV + indices for explore/search tests."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import Security

    securities = [
        {"symbol": "RELIANCE", "name": "Reliance Industries", "instrument_type": "stock", "is_active": True},
        {"symbol": "NIFTYBEES", "name": "Nippon India ETF Nifty 50 BeES", "instrument_type": "etf", "is_active": True},
        {"symbol": "INF204K01MY2", "name": "Parag Parikh Flexi Cap Fund", "instrument_type": "mf", "is_active": True},
    ]
    with SessionLocal() as session:
        for row in securities:
            stmt = sqlite_insert(Security).values(**row)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Security.symbol],
                    set_={k: v for k, v in row.items() if k != "symbol"},
                )
            )
        session.commit()

    base = datetime(2020, 1, 1)
    market_db.merge_ohlcv(
        pl.DataFrame(
            {
                "symbol": ["RELIANCE", "NIFTYBEES", "INF204K01MY2"] * 2,
                "ts": [base] * 3 + [base + timedelta(days=365)] * 3,
                "open": [100.0, 10.0, None, 110.0, 12.0, None],
                "high": [101.0, 10.5, None, 111.0, 12.5, None],
                "low": [99.0, 9.5, None, 109.0, 11.5, None],
                "close": [100.5, 10.2, 50.0, 110.5, 12.2, 60.0],
                "volume": [1_000, 5_000, 0, 2_000, 6_000, 0],
                "series": ["EQ", "EQ", "MF", "EQ", "EQ", "MF"],
            }
        )
    )
    market_db.upsert_indices(
        pl.DataFrame(
            {
                "symbol": ["NIFTY_50"] * 2,
                "ts": [base, base + timedelta(days=365)],
                "close": [12000.0, 14000.0],
            }
        )
    )


def test_tickers_search(client: TestClient) -> None:
    from app.engine.db import reset_coverage_cache

    _seed_ticker_world()
    reset_coverage_cache()
    res = client.get("/api/market/tickers", params={"q": "rel"})
    assert res.status_code == 200
    rows = res.json()
    assert rows, "expected at least one match"
    top = rows[0]
    assert top["symbol"] == "RELIANCE"
    assert top["type"] == "stock"
    assert top["date_first_entry"] == "2020-01-01"
    assert top["date_last_entry"] == "2020-12-31"


def test_tickers_type_filter_and_index(client: TestClient) -> None:
    from app.engine.db import reset_coverage_cache

    _seed_ticker_world()
    reset_coverage_cache()

    rows = client.get("/api/market/tickers", params={"q": "", "types": "index"}).json()
    assert rows and all(r["type"] == "index" for r in rows)
    assert any(r["symbol"] == "NIFTY_50" for r in rows)

    rows = client.get("/api/market/tickers", params={"q": "bees", "types": "etf"}).json()
    assert any(r["symbol"] == "NIFTYBEES" and r["type"] == "etf" for r in rows)

    rows = client.get("/api/market/tickers", params={"q": "Parag", "types": "mf"}).json()
    assert any(r["symbol"] == "INF204K01MY2" and r["type"] == "mf" for r in rows)


def test_overview_by_type(client: TestClient) -> None:
    from app.engine.db import reset_coverage_cache

    _seed_ticker_world()
    reset_coverage_cache()

    body = client.get("/api/market/overview/NIFTY_50").json()
    assert body["type"] == "index"
    assert body["price"] == 14000.0
    assert set(body["returns"]) >= {"ytd", "1y", "3y", "5y", "10y"}

    body = client.get("/api/market/overview/INF204K01MY2").json()
    assert body["type"] == "mf"
    assert body["price"] == 60.0

    body = client.get("/api/market/overview/RELIANCE").json()
    assert body["type"] == "stock"
    assert body["price"] == 110.5


def test_close_on_endpoint(client: TestClient) -> None:
    _seed_ticker_world()
    res = client.get("/api/market/close-on", params={"symbol": "RELIANCE", "day": "2020-01-05"})
    assert res.status_code == 200
    assert res.json()["close"] == 100.5

    res = client.get("/api/market/close-on", params={"symbol": "UNKNOWN", "day": "2020-01-05"})
    assert res.json()["close"] is None


def test_add_transaction_price_fallback(client: TestClient) -> None:
    _seed_ticker_world()
    res = client.post("/api/portfolios", json={"name": "fallback-portfolio"})
    assert res.status_code == 201
    pid = res.json()["id"]

    res = client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": "2021-01-01", "symbol": "reliance", "side": "buy", "quantity": 10, "price": 0},
    )
    assert res.status_code == 201
    assert res.json()["price"] == 110.5

    res = client.post(
        f"/api/portfolios/{pid}/transactions",
        json={"trade_date": "2020-01-04", "symbol": "RELIANCE", "side": "buy", "quantity": 5, "price": 0},
    )
    assert res.status_code == 201
    assert res.json()["price"] == 100.5


def test_import_transactions_csv(client: TestClient) -> None:
    _seed_ticker_world()
    res = client.post("/api/portfolios", json={"name": "import-portfolio"})
    assert res.status_code == 201
    pid = res.json()["id"]

    csv_content = (
        "Date,Symbol,Side,Quantity,Price,Fees,Note\n"
        "2021-01-01,RELIANCE,buy,10,110.50,20.5,first\n"
        "2020-01-04,NIFTYBEES,buy,100,0,0,zero price auto-filled\n"
        "2021-01-01,RELIANCE,buy,10,110.5,20.5,dup\n"
        "2020-01-10,UNKNOWN,buy,5,0,0,no data\n"
        "bogus,row,here,1,1,0,bad\n"
    )
    res = client.post(
        f"/api/portfolios/{pid}/transactions/import",
        files={"file": ("txns.csv", csv_content.encode(), "text/csv")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["imported"] == 2
    reasons = {s["reason"] for s in body["skipped"]}
    assert any("duplicate" in r for r in reasons)
    assert any("no price data" in r for r in reasons)
    assert any("unparseable date" in r for r in reasons)

    res = client.get(f"/api/portfolios/{pid}/transactions")
    txns = res.json()
    assert len(txns) == 2
    reliance = next(t for t in txns if t["symbol"] == "RELIANCE")
    assert reliance["price"] == 110.5
    assert reliance["note"] == "first"
    bees = next(t for t in txns if t["symbol"] == "NIFTYBEES")
    assert bees["price"] == 10.2

    res = client.post(
        f"/api/portfolios/{pid}/transactions/import",
        files={"file": ("txns.csv", csv_content.encode(), "text/csv")},
    )
    assert res.status_code == 200
    assert res.json()["imported"] == 0
