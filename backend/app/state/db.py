import json
import logging
import sqlite3
from datetime import date
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_connect_args = (
    {"check_same_thread": False} if settings.effective_database_url.startswith("sqlite") else {}
)

engine = create_engine(
    settings.effective_database_url,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app.state import models

    _migrate_sqlite()
    Base.metadata.create_all(bind=engine)
    _finish_portfolio_migration()


def _sqlite_path() -> str:
    path = urlparse(settings.effective_database_url).path.removeprefix("/")
    return path or "mehl.db"


def _migrate_sqlite() -> None:
    """Lightweight migrations for existing SQLite databases.

    create_all only creates missing tables; it never alters existing ones.
    Handles: additive nullable columns on existing tables, and the legacy
    portfolios.positions JSON column (rebuilt after create_all).
    """
    if not settings.effective_database_url.startswith("sqlite"):
        return
    path = _sqlite_path()
    import os

    if not os.path.exists(path):
        return
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _add_missing_columns(con, tables)
        if "portfolios" in tables:
            columns = {r[1] for r in con.execute("PRAGMA table_info(portfolios)")}
            if "positions" in columns:
                _migrate_positions_to_transactions(con)
    finally:
        con.close()


def _add_missing_columns(con: sqlite3.Connection, tables: set[str]) -> None:
    expected = {
        "securities": [("amfi_code", "VARCHAR")],
    }
    for table, columns in expected.items():
        if table not in tables:
            continue
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, col_type in columns:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")
    con.commit()


def _migrate_positions_to_transactions(con: sqlite3.Connection) -> None:
    """Move legacy positions JSON into transactions, then park the old
    portfolios table aside so create_all can recreate it without the column."""
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'").fetchone():
        con.execute(
            "CREATE TABLE transactions ("
            "id INTEGER PRIMARY KEY, portfolio_id INTEGER, trade_date DATE, symbol VARCHAR, "
            "isin VARCHAR, side VARCHAR, quantity FLOAT, price FLOAT, fees FLOAT, note VARCHAR, "
            "created_at DATETIME)"
        )
    rows = con.execute("SELECT id, positions, created_at FROM portfolios").fetchall()
    for portfolio_id, positions_json, created_at in rows:
        try:
            positions = json.loads(positions_json) if positions_json else []
        except (TypeError, ValueError):
            positions = []
        for p in positions:
            qty = float(p.get("quantity") or 0)
            if qty <= 0:
                continue
            trade_date = str(created_at or "")[:10] or date.today().isoformat()
            con.execute(
                "INSERT INTO transactions (portfolio_id, trade_date, symbol, side, quantity, price, fees) "
                "VALUES (?, ?, ?, 'buy', ?, ?, 0)",
                (
                    portfolio_id,
                    trade_date,
                    str(p.get("symbol") or "").upper(),
                    qty,
                    float(p.get("avg_cost") or 0),
                ),
            )
    con.execute("DROP TABLE IF EXISTS portfolios_premigration")
    con.execute("ALTER TABLE portfolios RENAME TO portfolios_premigration")
    con.commit()
    logger.info("migrated %d legacy portfolios to transactions ledger", len(rows))


def _finish_portfolio_migration() -> None:
    """Copy pre-migration portfolios into the recreated table, then drop the old one."""
    if not settings.effective_database_url.startswith("sqlite"):
        return
    import os

    path = _sqlite_path()
    if not os.path.exists(path):
        return
    con = sqlite3.connect(path)
    try:
        parked = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='portfolios_premigration'"
        ).fetchone()
        if not parked:
            return
        con.execute(
            "INSERT OR IGNORE INTO portfolios (id, name, base_currency, rebalancing_rules, created_at) "
            "SELECT id, name, base_currency, rebalancing_rules, created_at FROM portfolios_premigration"
        )
        con.execute("DROP TABLE portfolios_premigration")
        con.commit()
    finally:
        con.close()
