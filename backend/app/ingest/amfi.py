import logging
import re
from datetime import date, datetime, timedelta

import polars as pl
import requests

from app.engine import db as market_db
from app.state.db import SessionLocal
from app.state.models import Security

logger = logging.getLogger(__name__)

NAV_ALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_SCHEME_URL = "https://api.mfapi.in/mf/{scheme}"
MFAPI_LIST_URL = "https://api.mfapi.in/mf"
# Single request returning the latest NAV for every scheme (~11 MB, ~2s). Preferred
# over the per-scheme /latest endpoint, which would cost one request per scheme.
MFAPI_BULK_LATEST_URL = "https://api.mfapi.in/mf/latest"
MFAPI_LATEST_URL = "https://api.mfapi.in/mf/{scheme}/latest"

# AMFI's NAV history report. Unlike www.amfiindia.com (which drops TCP on 443
# from this host) this resolves to 14.143.46.157 and serves current data, so it is
# the only reachable AMFI-native source and the authority for NAVs.
PORTAL_HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
# tp selects the report type, and all three are required: tp=1 is open-ended only
# and omits close-ended and interval schemes, which is ~176 tracked codes.
PORTAL_TP = (1, 2, 3)
# The report caps the requested date window. Measured 2026-09-27: 11 days
# returned data reliably, 30 days failed 1 run in 3, 47+ days failed every time.
PORTAL_CHUNK_DAYS = 7
PORTAL_TIMEOUT = 120
# Window for scheme discovery. Only a scheme publishing *recently* needs to be
# seen, so a short window is enough and a wide one only costs requests.
SCHEME_SYNC_DAYS = 14
# A too-wide window is NOT a 4xx. AMFI answers 200 with a ~13.7 kB HTML form
# page containing this marker, so the body must be validated or a backfill
# silently stores nothing while reporting success.
PORTAL_ERROR_MARKER = "Application Error!"
_PORTAL_HEADER = "Scheme Code;NAV Name"
# strftime("%b") follows the process locale; AMFI always uses English months.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# A NAV snapshot is only merged when its newest row is this recent. AMFI's
# api. host answers 200 with a snapshot frozen at 2022-01-23, so without
# this guard a fallback ladder would happily merge four-year-old NAVs.
NAV_MIN_FRESHNESS_DAYS = 7

# Schemes fetched per DuckDB write batch. Higher = fewer connect/disconnect
# cycles, but the write lock is held for the length of one batch's inserts.
MF_INSERT_BATCH = 50

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

_EMPTY = pl.DataFrame(
    schema={
        "symbol": pl.String,
        "ts": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
        "series": pl.String,
        "instrument_type": pl.String,
        "name": pl.String,
        "amfi_code": pl.String,
    }
)


def _classify(name: str) -> str:
    upper = name.upper()
    if "ETF" in upper:
        return "etf"
    return "mf"


def parse_nav_all(content: str) -> pl.DataFrame:
    rows: list[dict] = []
    for line in content.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6:
            continue
        code, isin_payout, isin_reinv, name, nav, nav_date = parts[:6]
        if not code.isdigit() or not nav or not name:
            continue
        isin = isin_payout if _ISIN_RE.match(isin_payout) else isin_reinv
        if not _ISIN_RE.match(isin):
            continue
        try:
            close = float(nav)
        except ValueError:
            # AMFI publishes "N.A." for schemes with no valuation that day. A
            # single such row used to abort the whole file.
            continue
        try:
            ts = datetime.strptime(nav_date, "%d-%b-%Y").date()
        except ValueError:
            continue
        rows.append(
            {
                "symbol": isin,
                "ts": ts,
                "open": None,
                "high": None,
                "low": None,
                "close": close,
                "volume": None,
                "series": "MF",
                "instrument_type": _classify(name),
                "name": name,
                "amfi_code": code,
            }
        )
    if not rows:
        return _EMPTY
    return pl.DataFrame(rows, schema=_EMPTY.schema)


def _reject_stale(
    df: pl.DataFrame, min_freshness_days: int | None = NAV_MIN_FRESHNESS_DAYS
) -> str | None:
    """Return a rejection reason when df carries no recent NAV, else None.

    Guards against a reachable-but-frozen source: AMFI's api. host serves HTTP
    200 with a 2022-01-23 snapshot.

    Pass min_freshness_days=None to skip the recency check entirely. Explicit
    historical ranges are legitimately old, and comparing them against today
    would reject every row of a correct backfill.
    """
    if min_freshness_days is None:
        return None
    if df.is_empty():
        return "no rows parsed"
    newest = df["ts"].max()
    cutoff = date.today() - timedelta(days=min_freshness_days)
    if newest is None or newest < cutoff:
        return f"newest NAV {newest} older than {min_freshness_days}d cutoff {cutoff}"
    return None


def _known_mf_codes() -> set[str]:
    """AMFI scheme codes we already track as MF/ETF securities."""
    from app.state.db import SessionLocal
    from app.state.models import Security

    with SessionLocal() as session:
        rows = (
            session.query(Security.amfi_code)
            .filter(Security.series == "MF", Security.amfi_code.isnot(None))
            .all()
        )
    return {str(r[0]) for r in rows if r[0]}


def _known_mf_pairs() -> list[tuple[str, str, str]]:
    """(amfi_code, isin, name) for every MF security we track."""
    from app.state.db import SessionLocal
    from app.state.models import Security

    with SessionLocal() as session:
        rows = (
            session.query(Security.amfi_code, Security.symbol, Security.name)
            .filter(Security.series == "MF", Security.amfi_code.isnot(None))
            .all()
        )
    return [(str(a), s, n or "") for a, s, n in rows if a]


def _drop_regressions(
    df: pl.DataFrame, force: bool = False
) -> tuple[pl.DataFrame, int]:
    """Drop rows at or before each symbol's stored high-water mark.

    merge_ohlcv lets incoming values win on conflict, so without this a
    reassigned scheme code (or a matured fund echoing an old date) could rewrite
    rows we already hold. Keeps daily runs strictly forward-only.

    force=True skips the guard so an authoritative source can restate a NAV.
    """
    if df.is_empty():
        return df, 0
    if force:
        return df, 0
    with market_db.get_connection() as con:
        marks = con.execute(
            "SELECT symbol, max(ts) AS max_ts FROM ohlcv WHERE series = 'MF' GROUP BY symbol"
        ).pl()
    if marks.is_empty():
        return df, 0
    joined = df.join(marks, on="symbol", how="left")
    kept = joined.filter(pl.col("max_ts").is_null() | (pl.col("ts") > pl.col("max_ts"))).drop(
        "max_ts"
    )
    return kept, df.height - kept.height


def _ingest_navall(
    min_freshness_days: int | None = NAV_MIN_FRESHNESS_DAYS, force: bool = False
) -> dict:
    """AMFI NAVAll.txt, the source of truth, with the stale-snapshot guard."""
    import requests

    res = requests.get(NAV_ALL_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    res.raise_for_status()
    df = parse_nav_all(res.text)
    reason = _reject_stale(df, min_freshness_days)
    if reason:
        logger.warning("nav-latest: amfi NAVAll rejected: %s", reason)
        return {
            "source": "amfi_navall",
            "parsed": df.height,
            "rows": 0,
            "rejected": reason,
            "latest_ts": None,
            "ok": False,
        }
    fresh, skipped = _drop_regressions(df, force)
    rows = market_db.merge_ohlcv(fresh) if not fresh.is_empty() else 0
    _sync_amfi_securities(df)
    return {
        "source": "amfi_navall",
        "parsed": df.height,
        "rows": rows,
        "skipped_regress": skipped,
        "rejected": None,
        "latest_ts": df["ts"].max(),
        "ok": not df.is_empty(),
    }


def ingest_amfi() -> int:
    """Ingest the latest NAVs from the AMFI NAVAll file (rows stored)."""
    result = _ingest_navall()
    if result["rejected"]:
        logger.warning("amfi: %s", result["rejected"])
        return 0
    logger.info(
        "amfi: %d scheme NAVs stored (%d skipped as stale)",
        result["rows"],
        result.get("skipped_regress", 0),
    )
    return result["rows"]


def _amfi_date(d: date) -> str:
    """Format a date the way the AMFI portal expects: DD-Mon-YYYY, English."""
    return "{:02d}-{}-{:04d}".format(d.day, _MONTHS[d.month - 1], d.year)


def _portal_body_error(content: str) -> str | None:
    """Return why an AMFI portal body is unusable, else None.

    A rejected request is still HTTP 200, carrying a ~13.7 kB HTML form page
    instead of the report. A status-code check alone would treat that as success
    and store zero rows, which is how a backfill can appear to run clean while
    writing nothing at all.
    """
    if PORTAL_ERROR_MARKER in content:
        return "portal returned an 'Application Error!' page (date window too wide?)"
    head = content.lstrip()[: len(_PORTAL_HEADER)]
    if head != _PORTAL_HEADER:
        return f"unexpected portal body starting {head[:48]!r}"
    return None


def fetch_portal_history(
    frmdt: date,
    todt: date,
    tp: int = 1,
    retries: int = 3,
    timeout: int = PORTAL_TIMEOUT,
) -> str:
    """Fetch one AMFI NAV history report, validating the body before returning."""
    import time

    import requests

    url = f"{PORTAL_HISTORY_URL}?tp={tp}&frmdt={_amfi_date(frmdt)}&todt={_amfi_date(todt)}"
    reason = "not attempted"
    for attempt in range(1, retries + 1):
        res = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        reason = _portal_body_error(res.text)
        if reason is None:
            return res.text
        logger.warning(
            "amfi portal tp=%s %s..%s attempt %d/%d: %s",
            tp,
            frmdt,
            todt,
            attempt,
            retries,
            reason,
        )
        if attempt < retries:
            time.sleep(min(2**attempt, 10))
    raise RuntimeError(f"amfi portal tp={tp} {frmdt}..{todt}: {reason}")


def parse_portal_history(
    content: str, known_codes: set[str] | None = None
) -> pl.DataFrame:
    """Parse a DownloadNAVHistoryReport_Po body into NAV rows.

    Layout is 8 semicolon fields: scheme code; name; plan; option; ISIN payout;
    ISIN reinvestment; NAV; date. Column 2 already carries plan and option, so the
    stored name lines up with what NAVAll.txt produces for the same scheme.
    Section headers and blank lines are skipped by the field-count test.
    """
    if known_codes is None:
        known_codes = _known_mf_codes()
    rows: list[dict] = []
    for line in content.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 8:
            continue
        code, name, _plan, _option, isin_payout, isin_reinv, nav, nav_date = parts[:8]
        if not code.isdigit() or not nav or not name:
            continue
        if known_codes and code not in known_codes:
            continue
        isin = isin_payout if _ISIN_RE.match(isin_payout) else isin_reinv
        if not _ISIN_RE.match(isin):
            continue
        try:
            close = float(nav)
        except ValueError:
            # "N.A." marks a scheme with no valuation that day.
            continue
        try:
            ts = datetime.strptime(nav_date, "%d-%b-%Y").date()
        except ValueError:
            continue
        rows.append(
            {
                "symbol": isin,
                "ts": ts,
                "open": None,
                "high": None,
                "low": None,
                "close": close,
                "volume": None,
                "series": "MF",
                "instrument_type": _classify(name),
                "name": name,
                "amfi_code": code,
            }
        )
    if not rows:
        return _EMPTY
    return pl.DataFrame(rows, schema=_EMPTY.schema)


def portal_chunks(start: date, end: date, days: int = PORTAL_CHUNK_DAYS):
    """Yield (from, to) windows of at most `days` days covering start..end."""
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def collect_portal_history(
    frmdt: date,
    todt: date,
    tps: tuple[int, ...] = PORTAL_TP,
    known_codes: set[str] | None = None,
    chunk_days: int | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """Fetch and parse portal history without writing anything.

    Returns the combined frame plus a list of per-request failures. A failing tp
    or chunk is recorded and skipped rather than aborting the range, so a single
    flaky response does not void an entire audit.
    """
    known = _known_mf_codes() if known_codes is None else known_codes
    windows = (
        [(frmdt, todt)] if chunk_days is None else list(portal_chunks(frmdt, todt, chunk_days))
    )
    frames: list[pl.DataFrame] = []
    errors: list[str] = []
    for tp in tps:
        for start, stop in windows:
            try:
                content = fetch_portal_history(start, stop, tp=tp)
            except Exception as e:
                errors.append(f"tp{tp} {start}..{stop}: {type(e).__name__}: {e}")
                logger.warning("amfi portal tp=%s %s..%s failed: %s", tp, start, stop, e)
                continue
            df = parse_portal_history(content, known_codes=known)
            if not df.is_empty():
                frames.append(df)
    if not frames:
        return _EMPTY, errors
    # tp categories are disjoint, but a scheme that changed category mid-window
    # can appear twice; keep one row per (symbol, ts).
    combined = pl.concat(frames, how="vertical_relaxed").unique(
        subset=["symbol", "ts"], keep="first"
    )
    return combined, errors


def ingest_portal_history(
    frmdt: date,
    todt: date,
    tps: tuple[int, ...] = PORTAL_TP,
    min_freshness_days: int | None = NAV_MIN_FRESHNESS_DAYS,
    known_codes: set[str] | None = None,
    force: bool = False,
) -> dict:
    """Store NAV history from the AMFI portal for the given date range."""
    df, errors = collect_portal_history(frmdt, todt, tps, known_codes)
    if min_freshness_days is not None:
        reason = _reject_stale(df, min_freshness_days)
        if reason:
            logger.warning("amfi portal rejected %s..%s: %s", frmdt, todt, reason)
            return {
                "source": "amfi_portal",
                "parsed": df.height,
                "rows": 0,
                "rejected": reason,
                "latest_ts": None,
                "ok": False,
                "errors": errors,
            }
    fresh, skipped = _drop_regressions(df, force)
    rows = market_db.merge_ohlcv(fresh) if not fresh.is_empty() else 0
    logger.info(
        "amfi portal %s..%s: %d parsed, %d merged, %d skipped as stale",
        frmdt,
        todt,
        df.height,
        rows,
        skipped,
    )
    return {
        "source": "amfi_portal",
        "parsed": df.height,
        "rows": rows,
        "skipped_regress": skipped,
        "rejected": None if not df.is_empty() else "no rows parsed",
        "latest_ts": df["ts"].max(),
        "ok": not df.is_empty(),
        "errors": errors,
    }


def ingest_portal_recent(
    days: int = PORTAL_CHUNK_DAYS,
    min_freshness_days: int | None = NAV_MIN_FRESHNESS_DAYS,
    force: bool = False,
) -> dict:
    """Primary daily NAV source: the last `days` of AMFI history, all three tp.

    A week of history per request is what keeps the window inside the range the
    report will actually serve, while still collecting more than a single day's
    NAVs so a missed day self-heals on the next run.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)
    result = ingest_portal_history(
        start, end, min_freshness_days=min_freshness_days, force=force
    )
    if result["ok"]:
        logger.info("amfi portal supplied %d rows for %s..%s", result["rows"], start, end)
    return result


def sync_schemes_from_portal(days: int = SCHEME_SYNC_DAYS) -> dict:
    """Register AMFI schemes we do not track yet, so new launches are not missed.

    Everything else in this module parses the portal *filtered* by the securities
    master, which means a scheme AMFI starts publishing is silently dropped until
    someone adds it by hand -- and the only code that used to add them
    (`_sync_amfi_securities` via `_ingest_navall`) sits on a ladder rung that
    cannot be reached while the portal works. This runs unfiltered on purpose.

    The window is short on purpose: we only need a scheme to be publishing
    *recently* to be discovered, and a wide window costs more requests for no
    extra discovery. The portal caps the usable range per request anyway.

    Only schemes AMFI is actively publishing get added, so this cannot import the
    long tail of dead schemes that mfapi.in carries.
    """
    end = date.today()
    # Inclusive, so `days=14` is 14 dates, not 15. The range is also chunked:
    # the portal answers a too-wide window with an HTTP 200 error page rather
    # than an error status, which shows up as zero rows.
    start = end - timedelta(days=days - 1)
    frame, errors = collect_portal_history(
        start, end, known_codes=None, chunk_days=PORTAL_CHUNK_DAYS
    )
    if frame.is_empty():
        logger.warning("scheme sync: portal returned nothing for %s..%s", start, end)
        return {
            "source": "amfi_portal",
            "seen": 0,
            "new_codes": 0,
            "new_isins": 0,
            "ok": False,
            "errors": errors,
        }

    known = _known_mf_codes()
    seen_codes = {str(c) for c in frame["amfi_code"].unique().to_list()}
    fresh = frame.filter(~pl.col("amfi_code").cast(pl.Utf8).is_in(known))
    if fresh.is_empty():
        logger.info("scheme sync: %d schemes seen, none new", len(seen_codes))
        return {
            "source": "amfi_portal",
            "seen": len(seen_codes),
            "new_codes": 0,
            "new_isins": 0,
            "ok": True,
            "errors": errors,
        }

    written = _sync_amfi_securities(fresh)
    new_codes = fresh["amfi_code"].n_unique()
    new_isins = fresh["symbol"].n_unique()
    logger.info(
        "scheme sync: %d schemes seen, %d new (%d ISINs, %d rows) added",
        len(seen_codes),
        new_codes,
        new_isins,
        written,
    )
    return {
        "source": "amfi_portal",
        "seen": len(seen_codes),
        "new_codes": new_codes,
        "new_isins": new_isins,
        "rows_written": written,
        "ok": True,
        "errors": errors,
    }


def audit_unknown_schemes() -> dict:
    """Report ISIN-bearing mfapi.in schemes we do not track. Never imports.

    mfapi.in is a strict superset of the AMFI active universe (every code AMFI
    publishes is present there), which makes it a useful cross-check: it also
    knows schemes that exist but have not started publishing NAVs on AMFI yet.
    That is exactly the window where a launch would otherwise go unnoticed.

    Warn-only by design. mfapi carries ~38k scheme codes of which ~29k are not in
    AMFI's active set, 28.6k have no ISIN at all, and it omits the payout/dividend
    ISIN. Importing from it would bloat the master with mostly-dormant entries.
    """
    response = requests.get(MFAPI_LIST_URL, timeout=60)
    response.raise_for_status()
    payload = response.json()
    remote: dict[str, str] = {}
    for entry in payload or []:
        if not isinstance(entry, dict):
            continue
        for key in ("isinGrowth", "isinDivReinvestment"):
            value = entry.get(key)
            if isinstance(value, str) and _ISIN_RE.match(value):
                remote.setdefault(value, str(entry.get("schemeCode") or ""))
                break

    with SessionLocal() as session:
        tracked = {
            row[0]
            for row in session.query(Security.symbol)
            .filter(Security.series == "MF")
            .all()
            if row[0]
        }
    unknown = sorted(set(remote) - tracked)
    logger.info(
        "scheme audit: %d ISINs known to mfapi, %d not tracked", len(remote), len(unknown)
    )
    return {
        "remote_isins": len(remote),
        "tracked_isins": len(tracked),
        "unknown": len(unknown),
        "unknown_sample": unknown[:25],
    }


def parse_mfapi_bulk_latest(
    payload: list, known_codes: set[str] | None = None
) -> pl.DataFrame:
    """Parse the mfapi.in /mf/latest bulk response into single-day NAV rows.

    The bulk response is flat -- one object per scheme with schemeCode,
    schemeName, isinGrowth/isinDivReinvestment, nav and date -- unlike the
    per-scheme endpoint which nests under "data". Schemes with a null nav are
    skipped, and when several codes map to one ISIN the first wins so a batch can
    never contain two rows for the same (symbol, ts).
    """
    rows: list[dict] = []
    seen_isins: set[str] = set()
    for entry in payload or []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("schemeCode") or "")
        if not code.isdigit():
            continue
        if known_codes is not None and code not in known_codes:
            continue
        isin = str(entry.get("isinGrowth") or entry.get("isinDivReinvestment") or "")
        if not _ISIN_RE.match(isin) or isin in seen_isins:
            continue
        name = str(entry.get("schemeName") or "")
        raw_nav = entry.get("nav")
        if not name or raw_nav is None or raw_nav == "":
            continue
        try:
            close = float(raw_nav)
            ts = datetime.strptime(str(entry.get("date")), "%d-%m-%Y").date()
        except (TypeError, ValueError):
            continue
        seen_isins.add(isin)
        rows.append(
            {
                "symbol": isin,
                "ts": ts,
                "open": None,
                "high": None,
                "low": None,
                "close": close,
                "volume": None,
                "series": "MF",
                "instrument_type": _classify(name),
                "name": name,
                "amfi_code": code,
            }
        )
    if not rows:
        return _EMPTY
    return pl.DataFrame(rows, schema=_EMPTY.schema)


def parse_mfapi_latest(payload: dict, isin: str, name: str) -> pl.DataFrame:
    """Parse one mfapi.in /mf/{scheme}/latest response into a single NAV row."""
    data = payload.get("data") or []
    if not data or not _ISIN_RE.match(isin or ""):
        return _EMPTY
    row = data[0] or {}
    try:
        close = float(row.get("nav"))
        ts = datetime.strptime(str(row.get("date")), "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return _EMPTY
    return pl.DataFrame(
        [
            {
                "symbol": isin,
                "ts": ts,
                "open": None,
                "high": None,
                "low": None,
                "close": close,
                "volume": None,
                "series": "MF",
                "instrument_type": _classify(name),
                "name": name,
                "amfi_code": None,
            }
        ],
        schema=_EMPTY.schema,
    )


def ingest_mfapi_latest(
    known_codes: set[str] | None = None,
    min_freshness_days: int | None = NAV_MIN_FRESHNESS_DAYS,
    force: bool = False,
) -> dict:
    """Fetch every scheme's latest NAV in ONE request via mfapi.in /mf/latest.

    Scoped to the amfi codes we already track so the tracked universe does not
    silently grow by every scheme mfapi lists.
    """
    import requests

    known = _known_mf_codes() if known_codes is None else known_codes
    res = requests.get(
        MFAPI_BULK_LATEST_URL, timeout=120, headers={"User-Agent": "Mozilla/5.0"}
    )
    res.raise_for_status()
    df = parse_mfapi_bulk_latest(res.json(), known_codes=known)
    reason = _reject_stale(df, min_freshness_days)
    if reason:
        logger.warning("nav-latest: mfapi bulk rejected: %s", reason)
        return {
            "source": "mfapi_bulk",
            "parsed": df.height,
            "rows": 0,
            "rejected": reason,
            "latest_ts": None,
            "ok": False,
        }
    fresh, skipped = _drop_regressions(df, force)
    rows = market_db.merge_ohlcv(fresh) if not fresh.is_empty() else 0
    logger.info(
        "mfapi-latest: %d parsed, %d merged, %d skipped as stale",
        df.height,
        rows,
        skipped,
    )
    return {
        "source": "mfapi_bulk",
        "parsed": df.height,
        "rows": rows,
        "skipped_regress": skipped,
        "rejected": None,
        "latest_ts": df["ts"].max(),
        "ok": True,
    }


def ingest_mfapi_latest_per_scheme(
    rate: float = 0.3,
    min_freshness_days: int | None = NAV_MIN_FRESHNESS_DAYS,
    force: bool = False,
) -> dict:
    """Last-resort fallback: one request per scheme via /mf/{scheme}/latest.

    Costs one request per tracked scheme (~9,200), so only used when the bulk
    endpoint is unavailable. Rows are written in batches.
    """
    from app.ingest.http import RateLimitedSession

    pairs = _known_mf_pairs()
    session = RateLimitedSession(min_interval=rate)
    pending: list[pl.DataFrame] = []
    parsed = skipped = 0
    frames: list[pl.DataFrame] = []
    for i, (code, isin, name) in enumerate(pairs, 1):
        try:
            res = session.get(MFAPI_LATEST_URL.format(scheme=code))
        except Exception as e:
            logger.warning("mfapi-latest %s: %s", code, e)
            continue
        if res.status_code != 200:
            continue
        frame = parse_mfapi_latest(res.json(), isin, name)
        if frame.is_empty():
            continue
        parsed += 1
        frames.append(frame)
        if len(frames) >= MF_INSERT_BATCH:
            pending.append(pl.concat(frames, how="diagonal_relaxed"))
            frames = []
        if i % 500 == 0:
            logger.info("mfapi-latest %d/%d schemes, %d parsed", i, len(pairs), parsed)
    if frames:
        pending.append(pl.concat(frames, how="diagonal_relaxed"))
    combined = pl.concat(pending, how="diagonal_relaxed") if pending else _EMPTY
    reason = _reject_stale(combined, min_freshness_days)
    if reason:
        logger.warning("nav-latest: mfapi per-scheme rejected: %s", reason)
        return {
            "source": "mfapi_scheme",
            "parsed": parsed,
            "rows": 0,
            "rejected": reason,
            "latest_ts": None,
            "ok": False,
        }
    fresh, skipped = _drop_regressions(combined, force)
    rows = market_db.merge_ohlcv(fresh) if not fresh.is_empty() else 0
    return {
        "source": "mfapi_scheme",
        "parsed": parsed,
        "rows": rows,
        "skipped_regress": skipped,
        "rejected": None,
        "latest_ts": combined["ts"].max(),
        "ok": parsed > 0,
    }


def ingest_nav_latest(
    min_freshness_days: int | None = NAV_MIN_FRESHNESS_DAYS,
    rate: float = 0.3,
    force: bool = False,
) -> dict:
    """Store the latest NAV for every tracked MF/ETF, best available source first.

    Ladder: AMFI portal history (authoritative, and the only AMFI host reachable
    here) -> AMFI NAVAll -> mfapi bulk /mf/latest -> mfapi per-scheme /latest.
    Each rung is stale-guarded, so a reachable but frozen snapshot is skipped
    rather than merged.

    A rung counts as satisfied on "ok", meaning it returned a usable payload --
    not on how many rows it happened to add. Gating on the row count instead made
    a correct no-op day fall through to the next rung, so an already-current
    database would fall all the way to the 9,200-request per-scheme fallback.

    With force=True the forward-only guard is bypassed, letting AMFI correct a
    NAV it restates.
    """
    errors: list[str] = []
    rungs = (
        ("amfi_portal", lambda: ingest_portal_recent(force=force)),
        ("amfi_navall", lambda: _ingest_navall(min_freshness_days, force=force)),
        (
            "mfapi_bulk",
            lambda: ingest_mfapi_latest(
                min_freshness_days=min_freshness_days, force=force
            ),
        ),
        (
            "mfapi_scheme",
            lambda: ingest_mfapi_latest_per_scheme(
                rate=rate, min_freshness_days=min_freshness_days, force=force
            ),
        ),
    )
    for name, fn in rungs:
        try:
            result = fn()
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            logger.warning("nav-latest: %s failed: %s", name, e)
            continue
        if result.get("ok"):
            result["errors"] = errors
            logger.info("nav-latest: %s supplied %d rows", name, result["rows"])
            return result
        errors.append(f"{name}: {result.get('rejected') or 'no rows'}")
    return {"source": None, "rows": 0, "ok": False, "errors": errors}


def _sync_amfi_securities(df: pl.DataFrame) -> int:
    from app.ingest.universe import sync_securities

    return sync_securities(
        df.select("symbol", "name", "series", "instrument_type", "amfi_code").with_columns(
            pl.lit(date.today()).alias("last_seen")
        )
    )


def parse_mfapi_history(payload: dict) -> pl.DataFrame:
    """Parse a mfapi.in /mf/{scheme} response into NAV rows."""
    meta = payload.get("meta") or {}
    scheme = str(meta.get("scheme_code") or "")
    isin = (
        meta.get("isin_growth")
        or meta.get("isin_div_reinvestment")
        or meta.get("isin_growth_reinvestment")
        or meta.get("isin_div_payout")
        or ""
    )
    name = str(meta.get("scheme_name") or "")
    rows: list[dict] = []
    if not scheme or not _ISIN_RE.match(isin):
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts": pl.Date,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "series": pl.String,
                "instrument_type": pl.String,
                "name": pl.String,
            }
        )
    for row in payload.get("data") or []:
        try:
            ts = datetime.strptime(str(row.get("date")), "%d-%m-%Y").date()
            close = float(row.get("nav"))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "symbol": isin,
                "ts": ts,
                "open": None,
                "high": None,
                "low": None,
                "close": close,
                "volume": None,
                "series": "MF",
                "instrument_type": _classify(name),
                "name": name,
            }
        )
    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts": pl.Date,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "series": pl.String,
                "instrument_type": pl.String,
                "name": pl.String,
            }
        )
    return pl.DataFrame(rows)


def fetch_mf_history(
    scheme: str, rate: float = 0.3, session: "RateLimitedSession | None" = None
) -> pl.DataFrame:
    """Fetch the full NAV history for one AMFI scheme code via mfapi.in."""
    from app.ingest.http import RateLimitedSession

    s = session or RateLimitedSession(min_interval=rate)
    res = s.get(MFAPI_SCHEME_URL.format(scheme=scheme))
    if res.status_code != 200:
        logger.warning("amfi-history: %s -> status %d", scheme, res.status_code)
        return pl.DataFrame()
    return parse_mfapi_history(res.json())


def ingest_mf_history(schemes: list[str], rate: float = 0.3) -> int:
    """Backfill full NAV history for the given AMFI scheme codes.

    Inserts are additive: existing NAV days are never overwritten. Rows are
    written in batches to avoid one DuckDB connection per scheme.
    """
    from app.ingest.http import RateLimitedSession

    total = 0
    session = RateLimitedSession(min_interval=rate)
    pending: list[pl.DataFrame] = []
    for i, scheme in enumerate(schemes, 1):
        df = fetch_mf_history(scheme, session=session)
        if df.is_empty():
            continue
        pending.append(df)
        if len(pending) >= MF_INSERT_BATCH:
            total += _insert_nav_batch(pending)
            pending = []
        if i % 100 == 0:
            logger.info("amfi-history %d/%d schemes, %d NAV rows so far", i, len(schemes), total)
    total += _insert_nav_batch(pending)
    return total
    return total


def all_scheme_codes() -> list[str]:
    """AMFI scheme codes known to the securities table, most recent sync first."""
    from app.state.db import SessionLocal
    from app.state.models import Security

    with SessionLocal() as session:
        rows = (
            session.query(Security.amfi_code)
            .filter(Security.amfi_code.isnot(None))
            .order_by(Security.last_seen.desc())
            .all()
        )
    return list(dict.fromkeys(r[0] for r in rows))


_EMPTY_LIST = pl.DataFrame(
    schema={
        "scheme_code": pl.String,
        "scheme_name": pl.String,
        "isin": pl.String,
    }
)


def parse_mfapi_list(payload: list) -> pl.DataFrame:
    """Parse the mfapi.in /mf scheme list into rows with valid ISINs."""
    rows: list[dict] = []
    for entry in payload or []:
        code = entry.get("schemeCode")
        isin = entry.get("isinGrowth") or entry.get("isinDivReinvestment") or ""
        name = entry.get("schemeName") or ""
        if not code or not _ISIN_RE.match(isin):
            continue
        rows.append({"scheme_code": str(code), "scheme_name": name, "isin": isin})
    if not rows:
        return _EMPTY_LIST
    return pl.DataFrame(rows, schema=_EMPTY_LIST.schema)


def sync_mfapi_securities(list_df: pl.DataFrame) -> int:
    """Upsert MF/ETF securities (symbol = ISIN) with amfi_code + names.

    One amfi_code per ISIN: the first scheme code wins; last_seen = today.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import Security

    if list_df.is_empty():
        return 0

    isin_to_code: dict[str, str] = {}
    names: dict[str, str] = {}
    for row in list_df.iter_rows(named=True):
        isin_to_code.setdefault(row["isin"], row["scheme_code"])
        names.setdefault(row["isin"], row["scheme_name"])

    with SessionLocal() as session:
        for isin, code in sorted(isin_to_code.items()):
            name = names.get(isin) or ""
            stmt = sqlite_insert(Security).values(
                symbol=isin,
                amfi_code=code,
                name=name,
                series="MF",
                instrument_type=_classify(name),
                last_seen=date.today(),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[Security.symbol],
                set_={
                    "amfi_code": stmt.excluded.amfi_code,
                    "name": stmt.excluded.name,
                    "series": stmt.excluded.series,
                    "instrument_type": stmt.excluded.instrument_type,
                    "last_seen": stmt.excluded.last_seen,
                },
            )
            session.execute(stmt)
        session.commit()
    return len(isin_to_code)


def fresh_mf_isins(max_age_days: int = 7) -> set[str]:
    """ISINs that already have NAV data from the last max_age_days (skip on
    re-runs). Based on actual NAV rows, not the securities sync timestamp."""
    from datetime import timedelta

    from app.engine import db as market_db

    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    with market_db.get_connection() as con:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM ohlcv WHERE series = 'MF' AND ts >= ?",
            [cutoff],
        ).fetchall()
    return {r[0] for r in rows}


def _insert_nav_batch(frames: list[pl.DataFrame]) -> int:
    """Insert a batch of per-scheme NAV frames in one connection + one statement.

    The caller keeps the connection short-lived (one batch at a time) so the
    single-writer DuckDB lock is not held for the whole network-bound backfill.
    """
    from app.engine.db import get_connection, insert_ohlcv

    if not frames:
        return 0
    combined = pl.concat(frames, how="diagonal_relaxed")
    with get_connection() as con:
        return insert_ohlcv(combined, con=con)


def ingest_mfapi_all(rate: float = 0.3, skip_fresh_days: int = 7) -> dict:
    """Backfill AMFI NAVs entirely via mfapi.in (amfiindia.com not required).

    One request per distinct ISIN: full NAV history (additive) plus the
    securities sync with amfi codes. Re-runs skip ISINs synced recently.
    Rows are written in batches so the whole run costs ~40 connections instead
    of one per scheme.
    """
    import requests

    from app.ingest.http import RateLimitedSession

    res = requests.get(MFAPI_LIST_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    res.raise_for_status()
    list_df = parse_mfapi_list(res.json())
    if list_df.is_empty():
        logger.warning("amfi-mfapi: no schemes with ISINs parsed")
        return {"schemes": 0, "isins": 0, "fetched": 0, "empty": 0, "nav_rows": 0}

    isin_count = sync_mfapi_securities(list_df)
    isin_to_code: dict[str, str] = {}
    for row in list_df.iter_rows(named=True):
        isin_to_code.setdefault(row["isin"], row["scheme_code"])

    skip = fresh_mf_isins(skip_fresh_days) if skip_fresh_days >= 0 else set()
    session = RateLimitedSession(min_interval=rate)
    fetched = empty = nav_rows = 0
    # one scheme can carry several ISINs; fetch each scheme code only once so a
    # symbol never appears twice in the same batch (duplicate (symbol, ts) keys)
    seen_codes: set[str] = set()
    targets: list[tuple[str, str]] = []
    for isin, code in sorted(isin_to_code.items()):
        if isin in skip or code in seen_codes:
            continue
        seen_codes.add(code)
        targets.append((isin, code))

    pending: list[pl.DataFrame] = []
    for i, (isin, code) in enumerate(targets, 1):
        df = fetch_mf_history(code, session=session)
        if df.is_empty():
            empty += 1
            continue
        pending.append(df)
        fetched += 1
        if len(pending) >= MF_INSERT_BATCH:
            nav_rows += _insert_nav_batch(pending)
            pending = []
        if i % 500 == 0:
            logger.info(
                "amfi-mfapi %d/%d: %d fetched, %d empty, %d nav rows",
                i, len(targets), fetched, empty, nav_rows,
            )
    nav_rows += _insert_nav_batch(pending)
    logger.info(
        "amfi-mfapi done: %d schemes, %d isins, %d fetched, %d empty, %d nav rows",
        len(list_df), isin_count, fetched, empty, nav_rows,
    )
    return {
        "schemes": len(list_df),
        "isins": isin_count,
        "fetched": fetched,
        "empty": empty,
        "nav_rows": nav_rows,
    }
