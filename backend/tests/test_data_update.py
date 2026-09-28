import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.engine import db as market_db
from app.main import app
from app.services import data_update


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fresh_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_update, "_lock", threading.Lock())


def _seed_family(name: str, series: str | None, when: date, symbols: int = 600) -> None:
    """Write a plausible block of bars for one data family."""
    rows = [
        {
            "symbol": f"FRESH{name}{i:04d}",
            "ts": f"{when.isoformat()} 00:00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0 + i,
            "volume": 1000.0,
            "series": series or "EQ",
        }
        for i in range(symbols)
    ]
    if name == "indices":
        market_db.insert_indices(
            pl.DataFrame(rows).select("symbol", "ts", "close").with_columns(
                pl.col("ts").str.to_datetime()
            )
        )
    else:
        market_db.merge_ohlcv(pl.DataFrame(rows))


@pytest.fixture(autouse=True)
def _seed_recent_families() -> None:
    """Every family carries a bar dated today, so a clean run really is clean."""
    today = date.today()
    # A healthy NAV day lists ~8,600 symbols, and the fund_navs floor is 6,000.
    _seed_family("equities", "EQ", today, symbols=600)
    _seed_family("funds", "MF", today, symbols=6_500)
    _seed_family("indices", None, today)


def _patch_sources(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    fail_bhavcopy: bool = False,
    bhavcopy_rows: int = 21_000,
) -> None:
    def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("nse blocked")

    monkeypatch.setattr(
        "app.ingest.bhavcopy.ingest_bhavcopy",
        boom if fail_bhavcopy else (lambda *a, **k: calls.append("bhavcopy") or bhavcopy_rows),
    )
    monkeypatch.setattr(
        "app.ingest.actions.ingest_actions", lambda *a, **k: calls.append("actions") or 3
    )
    monkeypatch.setattr(
        "app.ingest.indices.ingest_indices", lambda *a, **k: calls.append("indices") or 2
    )
    # Scheme discovery opens the AMFI portal; the weekly audit pulls ~6 MB from
    # mfapi.in. Neither may touch the network in tests.
    monkeypatch.setattr(
        "app.ingest.amfi.sync_schemes_from_portal",
        lambda *a, **k: calls.append("scheme_sync")
        or {"source": "amfi_portal", "seen": 10, "new": 0, "ok": True, "errors": []},
    )
    monkeypatch.setattr(
        "app.services.data_update._run_scheme_audit",
        lambda: calls.append("scheme_audit") or {"skipped": True},
    )
    # Never let the update path reach the network in tests: the NAV ladder opens
    # with a 60s AMFI timeout against a host that is currently unreachable.
    monkeypatch.setattr(
        "app.ingest.amfi.ingest_nav_latest",
        lambda *a, **k: calls.append("navs")
        or {"rows": 100, "source": "mfapi_bulk", "ok": True, "errors": []},
    )


def test_run_data_update_happy_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)

    result = data_update.run_data_update()
    assert result["status"] == "ok"
    assert result["steps"] == {
        "bhavcopy": 21_000,
        "actions": 3,
        "indices": 2,
        "scheme_sync": {"source": "amfi_portal", "seen": 10, "new": 0, "ok": True, "errors": []},
        "scheme_audit": {"skipped": True},
        "navs": {"rows": 100, "source": "mfapi_bulk", "ok": True, "errors": []},
    }
    assert result["errors"] == []
    assert result["warnings"] == []
    assert not data_update.is_running()
    # scheme_sync must precede navs: the portal ingest is filtered by the
    # securities master, so a scheme discovered in this run only gets NAV rows
    # if it was registered first.
    assert calls == ["bhavcopy", "actions", "indices", "scheme_sync", "scheme_audit", "navs"]
    assert data_update.read_status()["status"] == "ok"


def test_run_data_update_warns_on_suspiciously_few_bhavcopy_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A near-empty bhavcopy pull means NSE refused most downloads -- that must
    not be reported as a clean run."""
    calls: list[str] = []
    _patch_sources(monkeypatch, calls, bhavcopy_rows=12)

    result = data_update.run_data_update()

    assert result["status"] == "partial"
    assert len(result["warnings"]) == 1
    assert "12 rows" in result["warnings"][0]
    assert result["errors"] == result["warnings"]


def test_run_data_update_captures_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_sources(monkeypatch, calls, fail_bhavcopy=True)

    result = data_update.run_data_update()
    assert result["status"] == "partial"
    assert any("bhavcopy" in e for e in result["errors"])
    assert result["steps"]["actions"] == 3
    assert "bhavcopy" not in result["steps"]


def test_run_data_update_reports_family_freshness(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run still reports how current each data family is."""
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)

    result = data_update.run_data_update()

    assert result["status"] == "ok"
    assert set(result["freshness"]) == {"equities", "fund_navs", "indices"}
    for info in result["freshness"].values():
        assert info["age_days"] == 0
        assert info["symbols"] >= info["min_symbols"]


def test_run_data_update_warns_when_a_family_goes_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the check: a source that stops responding must not be
    reported as a successful update just because the other steps returned rows."""
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)
    monkeypatch.setattr(
        data_update,
        "family_freshness",
        lambda: {
            "equities": {"last_ts": "2026-09-25", "symbols": 2600, "age_days": 0, "min_symbols": 500},
            "fund_navs": {"last_ts": "2026-08-20", "symbols": 8000, "age_days": 38, "min_symbols": 6000},
            "indices": {"last_ts": "2026-09-25", "symbols": 27, "age_days": 0, "min_symbols": 5},
        },
    )

    result = data_update.run_data_update()

    assert result["status"] == "partial"
    assert len(result["warnings"]) == 1
    assert "fund_navs" in result["warnings"][0]
    assert "38 days old" in result["warnings"][0]
    assert result["errors"] == result["warnings"]


def test_freshness_warnings_flags_empty_and_thin_families() -> None:
    warnings = data_update.freshness_warnings(
        {
            "equities": {"last_ts": None, "symbols": 0, "age_days": None, "min_symbols": 500},
            "fund_navs": {"last_ts": "2026-09-25", "symbols": 4, "age_days": 0, "min_symbols": 6000},
            "indices": {"last_ts": "2026-09-25", "symbols": 27, "age_days": 0, "min_symbols": 5},
        }
    )
    assert any("equities: no data" in w for w in warnings)
    assert any("only 4 symbols" in w and "partial source file" in w for w in warnings)
    assert not any("indices" in w for w in warnings)


def test_run_data_update_lock_prevents_concurrency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)

    with data_update._lock:
        result = data_update.run_data_update()
    assert result["status"] == "already_running"
    assert calls == []


def test_update_status_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = client.get("/api/data-update/status")
    assert res.status_code == 200
    body = res.json()
    assert "running" in body
    assert "last_run" in body

    calls: list[str] = []

    def fake_run() -> dict:
        calls.append("run")
        return {"status": "ok"}

    monkeypatch.setattr(data_update, "run_data_update", fake_run)
    res = client.post("/api/data-update/run")
    assert res.status_code == 200
    assert res.json()["status"] in ("started", "already_running")


def test_freshness_counts_symbols_across_the_window_not_just_newest_date() -> None:
    """A few funds pricing a day ahead must not look like a partial source file.

    Overnight and liquid funds publish on a different calendar than the rest, so
    max(ts) lands on today while most of the universe still holds yesterday's NAV.
    Counting only max(ts) reported a few hundred symbols on a perfectly healthy
    day and masked a genuine partial file.
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    _seed_family("fundahead", "MF", today, symbols=650)
    _seed_family("fundbulk", "MF", yesterday, symbols=8_000)

    info = data_update.family_freshness()["fund_navs"]

    assert info["last_ts"] == today.isoformat()
    assert info["age_days"] == 0
    # both the 650 ahead-of-the-bulk and the 8,000 on yesterday count as current
    assert info["symbols"] >= 8_600
    assert not [
        w
        for w in data_update.freshness_warnings({"fund_navs": info})
        if "fund_navs" in w
    ]


def test_run_data_update_flags_a_failed_nav_ladder(client: TestClient, monkeypatch) -> None:
    """A ladder that exhausted every rung must not report success.

    Collapsing the ladder result to a row count made a total failure look like a
    quiet day, because the fallback always yields a number.
    """
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)
    monkeypatch.setattr(
        "app.ingest.amfi.ingest_nav_latest",
        lambda *a, **k: {"rows": 0, "source": None, "ok": False,
                         "errors": ["amfi_portal: 500", "mfapi_bulk: 500"]},
    )

    result = data_update.run_data_update()
    assert result["status"] == "partial"
    assert any("no source produced NAVs" in e for e in result["errors"])


def test_run_data_update_reports_new_schemes(client: TestClient, monkeypatch) -> None:
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)
    monkeypatch.setattr(
        "app.ingest.amfi.sync_schemes_from_portal",
        lambda *a, **k: calls.append("scheme_sync")
        or {"source": "amfi_portal", "seen": 40, "new_codes": 1,
            "new_isins": 2, "rows_written": 2, "ok": True, "errors": []},
    )

    result = data_update.run_data_update()
    assert result["steps"]["scheme_sync"]["new_codes"] == 1
    assert result["status"] == "ok"
    assert calls.index("scheme_sync") < calls.index("navs")


def test_run_data_update_warns_on_unknown_schemes_without_importing(
    client: TestClient, monkeypatch
) -> None:
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)
    monkeypatch.setattr(
        "app.services.data_update._run_scheme_audit",
        lambda: {"remote_isins": 12_886, "tracked_isins": 9_248, "unknown": 3_638,
                 "unknown_sample": ["INE009A01021"]},
    )

    result = data_update.run_data_update()
    # A large unknown count is expected and is not an error.
    assert result["status"] == "ok"
    assert result["errors"] == []
    assert any("3,638" in w or "3638" in w for w in result["warnings"])


def test_needs_catchup_is_false_after_a_fresh_success(monkeypatch) -> None:
    monkeypatch.setattr(
        data_update, "read_status",
        lambda: {"status": "ok", "last_run_at": datetime.now(UTC).isoformat()},
    )
    assert data_update.needs_catchup() is False


def test_needs_catchup_is_true_when_stale_missing_or_failed(monkeypatch) -> None:
    stale = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    monkeypatch.setattr(
        data_update, "read_status", lambda: {"status": "ok", "last_run_at": stale}
    )
    assert data_update.needs_catchup() is True

    monkeypatch.setattr(data_update, "read_status", lambda: None)
    assert data_update.needs_catchup() is True, "never run before"

    monkeypatch.setattr(
        data_update, "read_status",
        lambda: {"status": "error", "last_run_at": datetime.now(UTC).isoformat()},
    )
    assert data_update.needs_catchup() is True, "a failure must be retried"


def test_status_survives_a_date_in_the_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A date anywhere in the result must not cost us the run's own status.

    The ladder result is persisted whole so a failed source stays visible after
    the fact, and `latest_ts` is a polars date. `Setting.value` is a JSON column,
    so this used to raise on save -- losing the status, the step timings and the
    error list of an otherwise successful run.
    """
    calls: list[str] = []
    _patch_sources(monkeypatch, calls)
    monkeypatch.setattr(
        "app.ingest.amfi.ingest_nav_latest",
        lambda *a, **k: {"rows": 5, "source": "amfi_portal", "ok": True,
                         "errors": [], "latest_ts": date(2026, 9, 25)},
    )

    data_update.run_data_update()

    stored = data_update.read_status()
    assert stored["status"] == "ok"
    assert stored["step_durations_s"]["navs"] >= 0
    assert stored["steps"]["navs"]["latest_ts"] == "2026-09-25"
