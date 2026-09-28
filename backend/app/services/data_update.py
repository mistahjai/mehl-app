"""Single data-update routine shared by the scheduler and the manual trigger.

Every ingest path is idempotent/additive (merge/upsert semantics,
UniqueConstraints, skip_fresh_days), so re-running never duplicates rows.
Only the triggers differ: an APScheduler cron job and the manual API button.
"""

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.engine import db as market_db
from app.engine.db import reset_coverage_cache
from app.state.db import SessionLocal
from app.state.models import Setting

logger = logging.getLogger(__name__)

STATUS_KEY = "data_update"
SCHEME_AUDIT_KEY = "scheme_audit"
SCHEME_AUDIT_INTERVAL_DAYS = 7

# A missed day self-heals on the next run, so scanning a long window only buys
# extra retry attempts against dates that will never exist. It was 7 days (5
# weekdays) at ~0.5s for a trading day but ~64s for a holiday, so every market
# holiday in range cost a minute of sleeping for nothing.
BHAVCOPY_DAYS = 3

_lock = threading.Lock()

# A healthy bhavcopy pull yields a few thousand rows per trading day. Fewer than
# this for the whole window means the source mostly failed (NSE blocks often),
# which is worth surfacing instead of silently reporting "ok".
BHAVCOPY_MIN_ROWS = 500

# Each data family is checked against the most recent bar it actually holds, so a
# family that silently stopped updating is reported instead of looking healthy.
# ETF units and fund NAVs are carried in the ohlcv table under series='MF'.
# The symbol floor is per family: a healthy equity day lists ~2,600 symbols and a
# NAV day ~8,600, but a full index update is only ever as many series as are mapped.
# The NAV floor is 6,000 rather than something low because a few hundred overnight
# and liquid funds price a day ahead of the rest: a low bar would be cleared by
# those alone while thousands of ordinary funds sat stale.
FRESHNESS_CHECKS: dict[str, tuple[str, str | None, int]] = {
    "equities": ("SELECT max(ts) FROM ohlcv WHERE series = 'EQ'", "EQ", 500),
    "fund_navs": ("SELECT max(ts) FROM ohlcv WHERE series = 'MF'", "MF", 6000),
    "indices": ("SELECT max(ts) FROM indices", None, 5),
}
FRESHNESS_MAX_AGE_DAYS = 4


def is_running() -> bool:
    return _lock.locked()


def needs_catchup(max_age_hours: float = 24.0) -> bool:
    """True when the last recorded run is missing or older than `max_age_hours`.

    The cron job only exists while the API process is running, so an outage that
    spans the trigger time loses the day with no trace. Called once at startup.
    """
    status = read_status()
    if not status:
        return True
    if status.get("status") not in ("ok", "partial"):
        return True
    last = status.get("last_run_at")
    if not last:
        return True
    try:
        ran_at = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ran_at).total_seconds() > max_age_hours * 3600


def read_status() -> dict:
    with SessionLocal() as session:
        setting = session.get(Setting, STATUS_KEY)
        return dict(setting.value) if setting else {}


def _json_safe(value: object) -> object:
    """Coerce a result tree into something the JSON settings column accepts.

    The whole ladder result is now persisted, not just its row count, which is
    what makes a failed NAV source visible afterwards. That result carries
    `date` objects (`latest_ts` is a polars date), and `Setting.value` is a JSON
    column, so serialising it verbatim raised and the run's own status was lost
    -- including the step timings.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _save_status(result: dict) -> None:
    with SessionLocal() as session:
        setting = session.get(Setting, STATUS_KEY)
        if setting is None:
            setting = Setting(key=STATUS_KEY, value={})
            session.add(setting)
        setting.value = _json_safe(result)
        session.commit()


def family_freshness(max_age_days: int = FRESHNESS_MAX_AGE_DAYS) -> dict[str, dict]:
    """Latest bar date and how many symbols are current, for each data family.

    Reported alongside every update so a family that has quietly stopped moving
    (a source that is now unreachable, or a partial file) shows up as a warning
    rather than passing as a successful run.

    `symbols` counts symbols with a bar inside the freshness window, not only
    those sitting on the single newest date. Fund NAVs need the window: a few
    hundred overnight and liquid funds price a day ahead of the rest, so
    max(ts) lands on today while most of the universe is still on yesterday's
    NAV. Counting only max(ts) would report a few hundred symbols on a healthy
    day and miss a genuine partial file.
    """
    out: dict[str, dict] = {}
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    with market_db.get_connection() as con:
        for name, (latest_sql, series, min_symbols) in FRESHNESS_CHECKS.items():
            last = con.execute(latest_sql).fetchone()[0]
            if last is None:
                out[name] = {
                    "last_ts": None,
                    "symbols": 0,
                    "age_days": None,
                    "min_symbols": min_symbols,
                }
                continue
            last_day = last.date()
            if series is None:
                symbols = con.execute(
                    "SELECT count(DISTINCT symbol) FROM indices WHERE ts >= ?", [cutoff]
                ).fetchone()[0]
            else:
                symbols = con.execute(
                    "SELECT count(DISTINCT symbol) FROM ohlcv "
                    "WHERE ts >= ? AND series = ?",
                    [cutoff, series],
                ).fetchone()[0]
            out[name] = {
                "last_ts": last_day.isoformat(),
                "symbols": int(symbols),
                "age_days": (date.today() - last_day).days,
                "min_symbols": min_symbols,
            }
    return out


def freshness_warnings(freshness: dict[str, dict]) -> list[str]:
    msgs: list[str] = []
    for name, info in sorted(freshness.items()):
        floor = info.get("min_symbols", 0)
        if info["last_ts"] is None:
            msgs.append(f"{name}: no data in the database")
        elif info["age_days"] > FRESHNESS_MAX_AGE_DAYS:
            msgs.append(
                f"{name}: newest data is {info['last_ts']} "
                f"({info['age_days']} days old, expected within {FRESHNESS_MAX_AGE_DAYS})"
            )
        elif info["symbols"] < floor:
            msgs.append(
                f"{name}: only {info['symbols']} symbols on {info['last_ts']}, "
                f"expected >= {floor} (partial source file?)"
            )
    return msgs


def _scheme_audit_due(now: date | None = None) -> bool:
    """True when the weekly mfapi completeness check has not run recently."""
    today = now or date.today()
    with SessionLocal() as session:
        setting = session.get(Setting, SCHEME_AUDIT_KEY)
        if setting is None or not isinstance(setting.value, dict):
            return True
        last = setting.value.get("at")
    if not last:
        return True
    try:
        return (today - date.fromisoformat(str(last)[:10])).days >= SCHEME_AUDIT_INTERVAL_DAYS
    except ValueError:
        return True


def _run_scheme_audit() -> dict:
    """Weekly warn-only completeness check against mfapi.in.

    Never imports anything: mfapi carries ~38k scheme codes of which ~29k are not
    in AMFI's active set, and it omits the payout/dividend ISIN. It is only a
    signal that a scheme may exist that we have never seen.
    """
    if not _scheme_audit_due():
        return {"skipped": True, "reason": f"ran within {SCHEME_AUDIT_INTERVAL_DAYS}d"}
    from app.ingest import amfi

    report = amfi.audit_unknown_schemes()
    report["at"] = date.today().isoformat()
    with SessionLocal() as session:
        setting = session.get(Setting, SCHEME_AUDIT_KEY)
        if setting is None:
            setting = Setting(key=SCHEME_AUDIT_KEY, value={})
            session.add(setting)
        setting.value = report
        session.commit()
    return report


def run_data_update(bhavcopy_days: int = BHAVCOPY_DAYS, rate: float = 0.5) -> dict:
    """Pull new data from every source and refresh derived caches.

    Steps run in order; a failing source (e.g. NSE blocked) is logged and
    skipped. Returns a status dict persisted in the Setting table.
    """
    if not _lock.acquire(blocking=False):
        return {"status": "already_running", "last_run": read_status()}
    started = time.monotonic()
    steps: dict[str, Any] = {}
    durations: dict[str, float] = {}
    errors: list[str] = []
    end = date.today()
    start = end - timedelta(days=bhavcopy_days)

    def step(name: str, fn: Any) -> None:
        began = time.monotonic()
        try:
            steps[name] = fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.warning("data update: %s failed: %s", name, e)
        durations[name] = round(time.monotonic() - began, 1)

    from app.ingest import actions as actions_mod
    from app.ingest import amfi, bhavcopy, indices as indices_mod

    step("bhavcopy", lambda: bhavcopy.ingest_bhavcopy(start, end, rate=rate))
    step("actions", lambda: actions_mod.ingest_actions(start, end, rate=rate))
    step("indices", lambda: indices_mod.ingest_indices(start, end))
    # Runs before navs on purpose: the portal ingest is filtered by the
    # securities master, so a scheme registered here gets its NAV rows in this
    # same run instead of waiting a day.
    step("scheme_sync", lambda: amfi.sync_schemes_from_portal())
    step("scheme_audit", _run_scheme_audit)
    # The ladder result is kept whole. Collapsing it to `["rows"]` meant a total
    # failure still recorded a number, so the run reported "ok" while the source
    # ladder had exhausted every fallback.
    step("navs", lambda: amfi.ingest_nav_latest())
    if not steps.get("navs", {}).get("ok"):
        errors.append(
            f"navs: no source produced NAVs ({steps.get('navs', {}).get('errors')})"
        )

    warnings: list[str] = []
    audit = steps.get("scheme_audit")
    if isinstance(audit, dict) and audit.get("unknown"):
        msg = (
            f"mfapi knows {audit['unknown']} ISINs we do not track (of "
            f"{audit.get('remote_isins', 0)}); most are dormant. Not imported by design."
        )
        warnings.append(msg)
        logger.info("data update: %s", msg)

    rows = steps.get("bhavcopy", 0)
    if isinstance(rows, int) and 0 < rows < BHAVCOPY_MIN_ROWS:
        msg = (
            f"bhavcopy only returned {rows} rows for the last {bhavcopy_days} days "
            f"(expected >= {BHAVCOPY_MIN_ROWS}); NSE likely refused most downloads"
        )
        warnings.append(msg)
        logger.warning("data update: %s", msg)
        errors.append(msg)

    freshness = family_freshness()
    for msg in freshness_warnings(freshness):
        warnings.append(msg)
        errors.append(msg)
        logger.warning("data update: %s", msg)

    reset_coverage_cache()

    duration = time.monotonic() - started
    result = {
        "status": "ok" if not errors else "partial",
        "last_run_at": datetime.now(UTC).isoformat(),
        "duration_s": round(duration, 1),
        "step_durations_s": durations,
        "steps": steps,
        "freshness": freshness,
        "warnings": warnings,
        "errors": errors,
    }
    _save_status(result)
    _lock.release()
    logger.info("data update finished: %s", result)
    return result
