import json
import logging
import re
from datetime import date, datetime, timedelta

import polars as pl

from app.engine.adjust import adjustment_factor_for, latest_action_date
from app.ingest.http import RateLimitedSession

logger = logging.getLogger(__name__)

ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"

SPLIT_RE = re.compile(
    r"split.*?rs\.?\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*(?:from\s+)?to\s+rs\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
BONUS_RE = re.compile(
    r"bonus\s*(?:ratio\s*)?:?\s*(\d+)\s*[:xX]\s*(\d+)",
    re.IGNORECASE,
)

_DATE_FORMATS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")


def parse_date_multi(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_purpose(purpose: str) -> tuple[str, float | None]:
    """Extract (action_type, adjustment_factor) from an NSE purpose string.

    Split: 'FACE VALUE SPLIT FROM RS.10/- TO RS.2/-' -> factor = old_fv/new_fv.
    Bonus: 'BONUS 1:1' -> factor = (a+b)/b.
    """
    m = SPLIT_RE.search(purpose)
    if m:
        old_fv, new_fv = float(m.group(1)), float(m.group(2))
        try:
            return "split", adjustment_factor_for("split", old_fv, new_fv)
        except ValueError:
            return "split", None
    m = BONUS_RE.search(purpose)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        try:
            return "bonus", adjustment_factor_for("bonus", float(a), float(b))
        except ValueError:
            return "bonus", None
    return "other", None


def parse_action_item(item: dict) -> dict | None:
    symbol = item.get("symbol")
    ex_date = parse_date_multi(item.get("exDate") or item.get("ex_date"))
    purpose = (item.get("subject") or item.get("purpose") or "").strip()
    if not symbol or not ex_date:
        return None
    action_type, factor = parse_purpose(purpose)
    return {
        "symbol": symbol.strip().upper(),
        "isin": item.get("isin") or None,
        "ex_date": ex_date,
        "action_type": action_type,
        "factor": factor,
        "purpose": purpose,
    }


def fetch_actions(start: date, end: date, rate: float = 0.5) -> pl.DataFrame:
    session = RateLimitedSession(min_interval=rate)
    session.bootstrap_nse_cookies()

    rows: list[dict] = []
    year = start.year
    while year <= end.year:
        year_start = max(start, date(year, 1, 1))
        year_end = min(end, date(year, 12, 31))
        params = {
            "index": "equities",
            "from_date": f"{year_start:%d-%m-%Y}",
            "to_date": f"{year_end:%d-%m-%Y}",
        }
        res = session.get(ACTIONS_URL, params=params)
        if res.status_code == 200 and res.content:
            try:
                items = json.loads(res.text)
            except json.JSONDecodeError as e:
                logger.warning("actions %d: invalid JSON: %s", year, e)
                items = []
            if isinstance(items, list):
                for item in items:
                    parsed = parse_action_item(item)
                    if parsed is not None:
                        rows.append(parsed)
                logger.info("actions %d: %d rows", year, len(items))
        else:
            logger.warning("actions %d: HTTP %s", year, res.status_code)
        year += 1

    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "isin": pl.String,
                "ex_date": pl.Date,
                "action_type": pl.String,
                "factor": pl.Float64,
                "purpose": pl.String,
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "symbol": pl.String,
            "isin": pl.String,
            "ex_date": pl.Date,
            "action_type": pl.String,
            "factor": pl.Float64,
            "purpose": pl.String,
        },
    )


def upsert_actions(df: pl.DataFrame) -> int:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.state.db import SessionLocal
    from app.state.models import CorporateAction

    if df.is_empty():
        return 0

    with SessionLocal() as session:
        for row in df.iter_rows(named=True):
            stmt = sqlite_insert(CorporateAction).values(
                symbol=row["symbol"],
                isin=row.get("isin"),
                ex_date=row["ex_date"],
                action_type=row["action_type"],
                factor=row.get("factor"),
                purpose=row.get("purpose") or "",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    CorporateAction.symbol,
                    CorporateAction.ex_date,
                    CorporateAction.action_type,
                    CorporateAction.purpose,
                ],
                set_={
                    "factor": stmt.excluded.factor,
                    "isin": stmt.excluded.isin,
                },
            )
            session.execute(stmt)
        session.commit()
    return df.height


def ingest_actions(start: date, end: date, rate: float = 0.5) -> int:
    df = fetch_actions(start, end, rate=rate)
    n = upsert_actions(df)
    splits = df.filter(pl.col("action_type") != "other").height if not df.is_empty() else 0
    logger.info("actions done: %d rows (%d splits/bonuses)", n, splits)
    return n


def resume_start_date(default: date = date(2000, 1, 1)) -> date:
    import sqlite3

    from app.config import settings

    path = settings.data_dir / "mehl.db"
    if not path.exists():
        return default
    con = sqlite3.connect(path)
    try:
        last = latest_action_date(con)
    finally:
        con.close()
    if last is None:
        return default
    return max(default, last - timedelta(days=30))
