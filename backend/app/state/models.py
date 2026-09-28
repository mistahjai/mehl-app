from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.state.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    base_currency: Mapped[str] = mapped_column(String, default="INR")
    rebalancing_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (CheckConstraint("side IN ('buy', 'sell')", name="ck_transaction_side"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, ForeignKey("portfolios.id"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    isin: Mapped[str | None] = mapped_column(String, default=None)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dividend(Base):
    __tablename__ = "dividends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, ForeignKey("portfolios.id"), index=True)
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    isin: Mapped[str | None] = mapped_column(String, default=None)
    amount_per_share: Mapped[float | None] = mapped_column(Float, default=None)
    total_amount: Mapped[float | None] = mapped_column(Float, default=None)
    note: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecurityInfo(Base):
    __tablename__ = "securities_info"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, unique=True, index=True)
    sector: Mapped[str | None] = mapped_column(String, default=None)
    industry: Mapped[str | None] = mapped_column(String, default=None)
    shares_outstanding: Mapped[float | None] = mapped_column(Float, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchlistItem(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    note: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class Screen(Base):
    __tablename__ = "screens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    filters: Mapped[list] = mapped_column(JSON, default=list)
    rank_by: Mapped[str | None] = mapped_column(String, default=None)
    rank_desc: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Security(Base):
    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, unique=True, index=True)
    isin: Mapped[str | None] = mapped_column(String, index=True, default=None)
    name: Mapped[str | None] = mapped_column(String, default=None)
    series: Mapped[str | None] = mapped_column(String, default=None)
    instrument_type: Mapped[str | None] = mapped_column(String, default=None)
    amfi_code: Mapped[str | None] = mapped_column(String, index=True, default=None)
    is_active: Mapped[bool] = mapped_column(default=True)
    listed_on: Mapped[date | None] = mapped_column(Date, default=None)
    last_seen: Mapped[date | None] = mapped_column(Date, default=None)


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (UniqueConstraint("symbol", "ex_date", "action_type", "purpose"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    isin: Mapped[str | None] = mapped_column(String, index=True, default=None)
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    action_type: Mapped[str] = mapped_column(String)
    factor: Mapped[float | None] = mapped_column(Float, default=None)
    purpose: Mapped[str] = mapped_column(String, default="")


class FinancialStatementRow(Base):
    __tablename__ = "financials"
    __table_args__ = (
        UniqueConstraint("symbol", "period_type", "period_end", "statement", "line_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    period_type: Mapped[str] = mapped_column(String)
    period_end: Mapped[date] = mapped_column(Date)
    statement: Mapped[str] = mapped_column(String)
    line_item: Mapped[str] = mapped_column(String)
    value: Mapped[float | None] = mapped_column(Float, default=None)
