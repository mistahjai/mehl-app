import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from app.config import settings
from app.ingest import actions as actions_mod
from app.ingest import amfi, bhavcopy, bhavcopy_dir, indices as indices_mod, universe
from app.ingest import yfinance_src, zip_archive, financial_coverage

logger = logging.getLogger(__name__)


def _run_universe(args: argparse.Namespace) -> None:
    n = universe.build_universe(rate=args.rate)
    print(f"universe: {n} instruments synced -> {settings.data_dir / 'mehl.db'}")


def _run_bhavcopy(args: argparse.Namespace) -> None:
    total = bhavcopy.ingest_bhavcopy(args.start, args.end, rate=args.rate)
    print(f"bhavcopy: {total} rows ingested into {settings.data_dir / 'market.duckdb'}")


def _run_amfi(args: argparse.Namespace) -> None:
    # Routes through the ladder rather than NAVAll.txt alone: www.amfiindia.com
    # drops TCP from this host, so calling ingest_amfi() directly always returned 0.
    result = amfi.ingest_nav_latest(force=args.force)
    if not result.get("ok"):
        print(f"amfi: no NAV source usable. attempts: {result.get('errors')}")
        return
    print(
        f"amfi: {result['rows']:,} NAV rows from {result['source']} "
        f"(latest {result.get('latest_ts')})"
    )
    for msg in result.get("errors") or []:
        print(f"  fallback: {msg}")


def _run_nav_latest(args: argparse.Namespace) -> None:
    result = amfi.ingest_nav_latest(rate=args.rate, force=args.force)
    if not result.get("ok"):
        print(f"nav-latest: no NAV source usable. attempts: {result.get('errors')}")
        return
    print(
        f"nav-latest: {result['rows']:,} NAV rows from {result['source']} "
        f"(parsed {result.get('parsed', 0):,}, skipped {result.get('skipped_regress', 0):,} "
        f"as stale, latest {result.get('latest_ts')})"
    )
    for msg in result.get("errors") or []:
        print(f"  fallback: {msg}")


def _run_nav_audit(args: argparse.Namespace) -> None:
    """Compare AMFI portal history against what we store.

    Read-only by default. With --backfill, inserts only the missing
    (symbol, ts) keys; existing values are never overwritten and nothing is
    deleted, so restatements still need a deliberate decision.
    """
    from app.ingest.nav_audit import audit_range, backfill_missing

    start = args.start or (date.today() - timedelta(days=args.days))
    end = args.end or date.today()
    report = audit_range(start, end, chunk_days=args.chunk_days)
    print(
        f"nav-audit: {start} .. {report['end']} "
        f"({report['chunks']} chunks x {len(report['tps'])} report types)"
    )
    print(
        f"  AMFI rows {report['amfi_rows']:,} | stored rows {report['stored_rows']:,} | "
        f"missing {report['missing']:,} | extra {report['extra']:,} | mismatched {report['mismatched']:,}"
    )
    for symbol, n in list(report["missing_by_symbol"].items())[:10]:
        print(f"  missing: {symbol} ({n} days)")
    for symbol, n in list(report["extra_by_symbol"].items())[:10]:
        print(f"  extra:   {symbol} ({n} days)")
    for row in report["mismatch_sample"][:10]:
        print(f"  nav differs: {row['symbol']} {row['ts']} amfi={row['amfi']} stored={row['stored']}")
    if report["errors"]:
        print(f"  {len(report['errors'])} request(s) failed:")
        for msg in report["errors"][:10]:
            print(f"    {msg}")

    if args.backfill:
        print()
        print("  backfilling missing keys (additive only, no overwrites, no deletes)")
        filled = backfill_missing(start, end, chunk_days=args.chunk_days, dry_run=args.dry_run)
        verb = "would insert" if args.dry_run else "inserted"
        print(
            f"  backfill: missing {filled['missing']:,} | {verb} {filled['inserted']:,}"
        )
        if filled["errors"]:
            print(f"  {len(filled['errors'])} request(s) failed during backfill:")
            for msg in filled["errors"][:10]:
                print(f"    {msg}")


def _run_amfi_history(args: argparse.Namespace) -> None:
    if args.isins:
        schemes = [s.strip() for s in args.isins.split(",") if s.strip()]
    else:
        schemes = amfi.all_scheme_codes()
    if not schemes:
        print("amfi-history: no scheme codes known; run `amfi` first")
        return
    total = amfi.ingest_mf_history(schemes, rate=args.rate)
    print(f"amfi-history: {total} NAV rows backfilled for {len(schemes)} schemes")


def _run_amfi_mfapi(args: argparse.Namespace) -> None:
    result = amfi.ingest_mfapi_all(rate=args.rate, skip_fresh_days=-1 if args.force else 7)
    print(
        f"amfi-mfapi: {result['schemes']:,} schemes, {result['isins']:,} isins, "
        f"{result['fetched']:,} fetched, {result['empty']:,} empty, {result['nav_rows']:,} nav rows"
    )


def _run_indices(args: argparse.Namespace) -> None:
    total = indices_mod.ingest_indices(args.start, args.end)
    print(f"indices: {total} rows ingested into {settings.data_dir / 'market.duckdb'}")


def _run_info(args: argparse.Namespace) -> None:
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = universe.all_symbols()
    rows = []
    for i, symbol in enumerate(symbols, 1):
        info = yfinance_src.fetch_security_info(symbol)
        if info is not None:
            rows.append(info)
        logger.info("info %d/%d %s: %s", i, len(symbols), symbol, "ok" if info else "skipped")
    n = yfinance_src.upsert_security_info(rows)
    print(f"info: {n} securities enriched")


def _run_actions(args: argparse.Namespace) -> None:
    n = actions_mod.ingest_actions(args.start, args.end, rate=args.rate)
    print(f"actions: {n} rows stored")


def _run_financials(args: argparse.Namespace) -> None:
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = universe.all_symbols()
    total = 0
    for i, symbol in enumerate(symbols, 1):
        df = yfinance_src.fetch_financials(symbol)
        total += yfinance_src.upsert_financials(df)
        logger.info("financials %d/%d %s: %d rows", i, len(symbols), symbol, df.height)
    print(f"financials: {total} rows stored")


def _run_fill_gaps(args: argparse.Namespace) -> None:
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = universe.all_symbols()
    total = 0
    for i, symbol in enumerate(symbols, 1):
        total += yfinance_src.fill_gaps_for_symbol(symbol, args.start, args.end, rate=args.rate)
        logger.info("fill-gaps %d/%d %s", i, len(symbols), symbol)
    print(f"fill-gaps: {total} rows added")


def _run_zip_archive(args: argparse.Namespace) -> None:
    path = args.path
    if not path:
        candidates = sorted(Path("backend").glob("*1990-2020.zip")) + sorted(
            Path(".").glob("india-stock-data*.zip")
        )
        path = candidates[0] if candidates else None
    if not path:
        print("import-zip: no zip found; pass --path /path/to/dataset.zip")
        return
    result = zip_archive.import_zip(Path(path))
    print(
        f"import-zip: {result['ohlcv_rows']:,} ohlcv rows, {result['index_rows']:,} index rows, "
        f"{result['securities_synced']:,} securities synced "
        f"({result['scrip_files']} scrip files, {result['index_files']} index files)"
    )


def _run_bhavcopy_dir(args: argparse.Namespace) -> None:
    path = Path(args.path)
    if not path.exists():
        print(f"import-bhavcopy-dir: path not found: {path}")
        return
    result = bhavcopy_dir.import_dir(path)
    print(
        f"import-bhavcopy-dir: {result['rows_merged']:,} rows merged across {result['files']} files, "
        f"{result['securities_synced']:,} securities synced"
    )


def _run_all(args: argparse.Namespace) -> None:
    logger.info("step 1/4: universe")
    universe.build_universe(rate=args.rate)
    logger.info("step 2/4: bhavcopy %s -> %s", args.start, args.end)
    bhavcopy.ingest_bhavcopy(args.start, args.end, rate=args.rate)
    logger.info("step 3/4: corporate actions")
    actions_mod.ingest_actions(args.start, args.end, rate=args.rate)
    logger.info("step 4/4: latest NAVs (AMFI, falling back to mfapi.in)")
    amfi.ingest_nav_latest()
    print("backfill complete")


def _run_update(args: argparse.Namespace) -> None:
    from app.services.data_update import run_data_update

    result = run_data_update(bhavcopy_days=args.days, rate=args.rate)
    print(f"update: {result['status']} in {result['duration_s']}s — steps: {result['steps']}")
    if result["errors"]:
        print(f"update errors: {result['errors']}")


def _run_fin_coverage(args: argparse.Namespace) -> None:
    from app.ingest.financial_coverage import (
        compute_coverage_report, export_csv, export_json, print_console_summary
    )

    report = compute_coverage_report(min_quarters_per_year=args.min_quarters)
    print_console_summary(report)

    if args.output:
        path = Path(args.output)
        export_csv(report, path.with_suffix('.csv'))
        export_json(report, path.with_suffix('.json'))
        print(f"\nExported: {path.with_suffix('.csv')} + {path.with_suffix('.json')}")

    if args.no_fin_csv:
        from app.ingest.financial_coverage import get_operating_without_financials_csv
        csv_content = get_operating_without_financials_csv()
        if args.no_fin_csv == "-":
            print(csv_content)
        else:
            Path(args.no_fin_csv).write_text(csv_content)
            print(f"Operating companies without financials CSV: {args.no_fin_csv}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from app.state.db import init_db

    init_db()

    parser = argparse.ArgumentParser(
        prog="mehl-ingest",
        description="mehl data ingestion: NSE bhavcopy, AMFI NAVs, corporate actions, yfinance",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_range(p: argparse.ArgumentParser, rate_default: float) -> None:
        p.add_argument("--start", type=date.fromisoformat, default=date(2000, 1, 1))
        p.add_argument("--end", type=date.fromisoformat, default=date.today())
        p.add_argument("--rate", type=float, default=rate_default, help="min seconds between requests")

    p = sub.add_parser("universe", help="build instrument master from the NSE equity list")
    p.add_argument("--rate", type=float, default=0.5)
    p.set_defaults(func=_run_universe)

    p = sub.add_parser("bhavcopy", help="backfill NSE bhavcopy OHLCV into DuckDB")
    add_range(p, 0.5)
    p.set_defaults(func=_run_bhavcopy)

    p = sub.add_parser(
        "amfi",
        help="ingest latest AMFI NAVs (MFs, debt funds, ETFs) via the portal, "
        "falling back to NAVAll.txt then mfapi.in",
    )
    p.add_argument("--force", action="store_true", help="allow AMFI to restate existing NAV rows")
    p.set_defaults(func=_run_amfi)

    p = sub.add_parser(
        "nav-latest",
        help="store the latest NAV for every tracked MF/ETF via the AMFI portal, "
        "falling back to NAVAll.txt then mfapi.in",
    )
    p.add_argument("--rate", type=float, default=0.3, help="min seconds between per-scheme requests")
    p.add_argument("--force", action="store_true", help="allow the source to restate existing NAV rows")
    p.set_defaults(func=_run_nav_latest)

    p = sub.add_parser(
        "nav-audit",
        help="compare AMFI portal NAV history against stored rows (read-only unless --backfill)",
    )
    p.add_argument("--start", type=date.fromisoformat, default=None, help="default: today - --days")
    p.add_argument("--end", type=date.fromisoformat, default=None, help="default: today")
    p.add_argument("--days", type=int, default=730, help="look-back window when --start is omitted")
    p.add_argument(
        "--chunk-days",
        type=int,
        default=amfi.PORTAL_CHUNK_DAYS,
        help="days per portal request; the report refuses wider windows",
    )
    p.add_argument(
        "--backfill",
        action="store_true",
        help="insert missing (symbol, ts) NAVs; never overwrites or deletes",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="with --backfill, report what would be inserted without writing",
    )
    p.set_defaults(func=_run_nav_audit)

    p = sub.add_parser(
        "amfi-history",
        help="backfill full NAV history per scheme via mfapi.in (additive)",
    )
    p.add_argument("--isins", type=str, default="", help="comma-separated AMFI scheme codes; default: all known")
    p.add_argument("--rate", type=float, default=0.3, help="min seconds between requests")
    p.set_defaults(func=_run_amfi_history)

    p = sub.add_parser(
        "amfi-mfapi",
        help="backfill all AMFI NAVs via mfapi.in only (no amfiindia.com needed)",
    )
    p.add_argument("--rate", type=float, default=0.25, help="min seconds between requests")
    p.add_argument("--force", action="store_true", help="refetch even ISINs with fresh NAVs")
    p.set_defaults(func=_run_amfi_mfapi)

    p = sub.add_parser("indices", help="backfill NIFTY 50/500 daily closes into DuckDB")
    add_range(p, 0.5)
    p.set_defaults(func=_run_indices)

    p = sub.add_parser(
        "info", help="fetch sector/industry/shares outstanding via yfinance"
    )
    p.add_argument("--symbols", type=str, default="", help="comma-separated NSE symbols; default: all")
    p.add_argument("--rate", type=float, default=1.0)
    p.set_defaults(func=_run_info)

    p = sub.add_parser("actions", help="fetch NSE corporate actions (splits/bonuses)")
    add_range(p, 0.5)
    p.set_defaults(func=_run_actions)

    p = sub.add_parser("financials", help="fetch quarterly/annual statements via yfinance")
    p.add_argument("--symbols", type=str, default="", help="comma-separated NSE symbols; default: all")
    p.add_argument("--rate", type=float, default=1.0)
    p.set_defaults(func=_run_financials)

    p = sub.add_parser("fill-gaps", help="backfill missing days via yfinance (raw prices)")
    add_range(p, 0.5)
    p.add_argument("--symbols", type=str, default="", help="comma-separated NSE symbols; default: all")
    p.set_defaults(func=_run_fill_gaps)

    p = sub.add_parser("all", help="full backfill: universe + bhavcopy + actions + amfi")
    add_range(p, 0.5)
    p.set_defaults(func=_run_all)

    p = sub.add_parser(
        "import-zip",
        help="import an NSE dataset zip (SCRIP/ + INDEX/ csvs) into DuckDB, additive",
    )
    p.add_argument("--path", type=str, default="", help="path to the zip; default: auto-detect *1990-2020.zip")
    p.set_defaults(func=_run_zip_archive)

    p = sub.add_parser(
        "import-bhavcopy-dir",
        help="import daily bhavcopy csvs (DDMMMYYYY.csv) from a directory, merge semantics (raw values win)",
    )
    p.add_argument("--path", type=str, required=True, help="path to the folder of daily csvs")
    p.set_defaults(func=_run_bhavcopy_dir)

    p = sub.add_parser(
        "update",
        help="daily data update: bhavcopy + actions + indices + AMFI NAVs, idempotent",
    )
    p.add_argument("--days", type=int, default=7, help="look-back window in days")
    p.add_argument("--rate", type=float, default=0.5, help="min seconds between requests")
    p.set_defaults(func=_run_update)

    p = sub.add_parser("fin-coverage", help="coverage report: financials vs OHLCV for mainboard operating companies")
    p.add_argument("--output", type=str, default="", help="output path (writes .csv + .json)")
    p.add_argument("--min-quarters", type=int, default=4, help="min quarters/year to meet threshold")
    p.add_argument("--no-fin-csv", type=str, default="", help="export CSV of operating companies without financials (use '-' for stdout)")
    p.set_defaults(func=_run_fin_coverage)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
