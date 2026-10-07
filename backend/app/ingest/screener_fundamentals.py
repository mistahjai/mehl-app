import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

import polars as pl
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import settings
from app.state.db import SessionLocal
from app.state.models import FinancialStatementRowScreener, Security

logger = logging.getLogger(__name__)

CR_TO_INR = 10_000_000

SECTION_MAP = {
    "profit_loss": ("profit_loss", "annual"),
    "profit_loss_consolidated": ("profit_loss", "annual"),
    "quarters": ("profit_loss", "quarterly"),
    "quarters_consolidated": ("profit_loss", "quarterly"),
    "balance_sheet": ("balance_sheet", "annual"),
    "balance_sheet_consolidated": ("balance_sheet", "annual"),
    "cash_flows": ("cash_flows", "annual"),
    "cash_flows_consolidated": ("cash_flows", "annual"),
    "other_ratios": ("ratios", "annual"),
    "other_ratios_consolidated": ("ratios", "annual"),
    "share_holding_pattern": ("share_holding", "annual"),
}

CONSOLIDATION_MAP = {
    "profit_loss": "standalone",
    "profit_loss_consolidated": "consolidated",
    "quarters": "standalone",
    "quarters_consolidated": "consolidated",
    "balance_sheet": "standalone",
    "balance_sheet_consolidated": "consolidated",
    "cash_flows": "standalone",
    "cash_flows_consolidated": "consolidated",
    "other_ratios": "standalone",
    "other_ratios_consolidated": "consolidated",
    "share_holding_pattern": "standalone",
}


def parse_period(period_str: str, period_type: str, all_periods: list[str] | None = None) -> tuple[date, str]:
    """Parse period string to (period_end, normalized_period_type).
    
    Returns: (date, period_type) where period_type can be 'annual', 'quarterly', 'ttm'
    """
    period_str = period_str.strip()
    
    if period_str.upper() == "TTM":
        if all_periods:
            # Find the latest quarter end from all_periods
            latest = None
            for p in all_periods:
                if p.upper() == "TTM":
                    continue
                parsed = parse_period(p, period_type)
                if parsed[0] and (latest is None or parsed[0] > latest):
                    latest = parsed[0]
            if latest:
                return latest, "ttm"
        return date.today(), "ttm"
    
    # Parse formats like "Mar 2025", "Jun 2025", "Sep 2025", "Dec 2025"
    # Or "Mar 2025", "2025-03-31", etc.
    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    
    parts = period_str.split()
    if len(parts) == 2:
        month_str, year_str = parts[0].lower()[:3], parts[1]
        if month_str in month_map and year_str.isdigit():
            month = month_map[month_str]
            year = int(year_str)
            # Quarter end dates
            if month in (3, 6, 9, 12):
                day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
            else:
                # For other months, use month end
                import calendar
                day = calendar.monthrange(year, month)[1]
            return date(year, month, day), period_type
    
    # Try ISO format
    try:
        return date.fromisoformat(period_str), period_type
    except ValueError:
        pass
    
    # Fallback
    return date.today(), period_type


def parse_value(val: str | float | int | None) -> float | None:
    """Parse value string to float in raw INR."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) * CR_TO_INR
    val = str(val).strip()
    if not val or val.lower() in ("-", "na", "n/a", "null"):
        return None
    # Remove commas, currency symbols, % signs
    val = val.replace(",", "").replace("₹", "").replace("%", "").replace(" ", "")
    try:
        return float(val) * CR_TO_INR
    except ValueError:
        return None


def get_active_mainboard_symbols() -> set[str]:
    """Get active mainboard (EQ/BE/BZ) stock symbols from securities table."""
    with SessionLocal() as session:
        rows = session.query(Security.symbol).filter(
            Security.series.in_(("EQ", "BE", "BZ")),
            Security.instrument_type == "stock",
            Security.is_active == True,
        ).all()
    return {r[0] for r in rows}


def process_json_file(file_path: Path, valid_symbols: set[str]) -> list[dict]:
    """Process a single JSON file and return rows for upsert."""
    try:
        with open(file_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read %s: %s", file_path, e)
        return []
    
    nse_symbol = data.get("profile", {}).get("nse_symbol")
    if not nse_symbol or nse_symbol not in valid_symbols:
        return []
    
    symbol = nse_symbol.strip().upper()
    rows = []
    
    for json_key, (statement, period_type) in SECTION_MAP.items():
        section = data.get(json_key)
        if not section:
            continue
        
        consolidation = CONSOLIDATION_MAP[json_key]
        years = section.get("year") or section.get("quarters", [])
        line_items = section.get("data", {})
        if not years or not line_items:
            continue
        
        # Get all periods for TTM resolution
        all_periods = years
        
        for line_item, values in line_items.items():
            if not values:
                continue
            for period_str, val in zip(years, values):
                parsed_val = parse_value(val)
                if parsed_val is None:
                    continue
                period_end, pt = parse_period(period_str, period_type, all_periods)
                rows.append({
                    "symbol": symbol,
                    "period_type": pt,
                    "period_end": period_end,
                    "statement": statement,
                    "line_item": line_item,
                    "value": parsed_val,
                    "consolidation": consolidation,
                    "source": "screener",
                })
    
    return rows


def upsert_batch(rows: list[dict]) -> int:
    """Batch upsert rows into financials_screener table."""
    if not rows:
        return 0
    
    with SessionLocal() as session:
        stmt = sqlite_insert(FinancialStatementRowScreener).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                FinancialStatementRowScreener.symbol,
                FinancialStatementRowScreener.period_type,
                FinancialStatementRowScreener.period_end,
                FinancialStatementRowScreener.statement,
                FinancialStatementRowScreener.line_item,
                FinancialStatementRowScreener.consolidation,
            ],
            set_={"value": stmt.excluded.value},
        )
        session.execute(stmt)
        session.commit()
    return len(rows)


def ingest_screener_fundamentals(
    data_dir: Path | None = None,
    batch_size: int = 10_000,
) -> dict:
    """Main ingestion function."""
    if data_dir is None:
        data_dir = settings.data_dir / "kaggle_fundamentals" / "extracted"
    
    if not data_dir.exists():
        logger.error("Data directory not found: %s", data_dir)
        return {"status": "error", "message": "Data directory not found"}
    
    valid_symbols = get_active_mainboard_symbols()
    logger.info("Found %d active mainboard symbols", len(valid_symbols))
    
    json_files = list(data_dir.rglob("*.json"))
    logger.info("Found %d JSON files", len(json_files))
    
    total_rows = 0
    processed = 0
    batch: list[dict] = []
    
    for file_path in json_files:
        rows = process_json_file(file_path, valid_symbols)
        batch.extend(rows)
        
        if len(batch) >= batch_size:
            upsert_batch(batch)
            total_rows += len(batch)
            batch = []
            processed += 1
            if processed % 100 == 0:
                logger.info("Processed %d files, %d rows inserted", processed, total_rows)
    
    # Flush remaining
    if batch:
        upsert_batch(batch)
        total_rows += len(batch)
    
    logger.info("Done: %d files processed, %d rows inserted", processed, total_rows)
    return {"files_processed": processed, "rows_inserted": total_rows}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = ingest_screener_fundamentals()
    print(result)