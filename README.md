# mehl

Stock and portfolio analytics for a single user, self-hosted on a remote machine and accessed over Tailscale.

## Architecture

```text
[ Laptop Browser / Mobile PWA ]
            |
       (Tailscale)
            v
+-----------------------------------------------+
|  Remote Machine (Docker Compose)              |
|                                               |
|  +-----------------------------------------+  |
|  | Container 1: Frontend (React/Vite/PWA)  |  |
|  |  - TradingView Lightweight Charts       |  |
|  |  - TanStack Table                       |  |
|  |  - Nginx reverse-proxy to FastAPI       |  |
|  +--------------------|--------------------+  |
|                       | REST / WebSocket     |
|  +--------------------v--------------------+  |
|  | Container 2: Backend (FastAPI + Python) |  |
|  |                                         |  |
|  |  Analytical Engine (DuckDB + Polars)    |  |
|  |   - Fast scans of 30M OHLCV rows        |  |
|  |   - Vectorized screener & backtesting   |  |
|  |                                         |  |
|  |  App State Engine (SQLite via SQLAlchemy)| |
|  |   - Transactions ledger, dividends,    |  |
|  |   - Portfolios, scenarios, watchlists  |  |
|  |   - Rebalancing rules, user settings   |  |
|  +-----------------------------------------+  |
+-----------------------------------------------+
```

- Only the frontend container is exposed (port 8080); Nginx proxies `/api/*` and `/ws` to the backend, so the backend never leaves the compose network.
- Market data lives in a DuckDB file (`backend/data/market.duckdb`), queried with DuckDB and processed with Polars (Arrow-backed, zero-copy).
- App state (portfolios, watchlists, scenarios, settings) lives in SQLite via SQLAlchemy; swap `MEHL_DATABASE_URL` to a PostgreSQL URL to move to Postgres later.

## Quickstart (Docker)

```sh
docker compose up --build
```

Then open `http://<remote-machine-ip>:8080` (or its Tailscale hostname). The API is reachable at `/api/health`, docs at `/api/docs`.

## Local development

Backend:

```sh
cd backend
uv sync
uv run pytest            # run tests
uv run uvicorn app.main:app --reload
```

Frontend (proxies `/api` and `/ws` to `localhost:8000`):

```sh
cd frontend
npm install --include=dev
npm run dev
```

## Configuration

Environment variables (prefix `MEHL_`, read by `backend/app/config.py`):

| Variable             | Default                        | Purpose                                |
| -------------------- | ------------------------------ | -------------------------------------- |
| `MEHL_DATA_DIR`      | `backend/data`                 | Where DuckDB + SQLite files live       |
| `MEHL_DATABASE_URL`  | SQLite in `MEHL_DATA_DIR`      | Set to a Postgres URL to switch stores |
| `MEHL_CORS_ORIGINS`  | `http://localhost:5173`        | Allowed CORS origins (dev)             |
| `MEHL_HOST` / `MEHL_PORT` | `0.0.0.0` / `8000`        | Uvicorn bind address                   |
| `MEHL_DATA_UPDATE_ENABLED` | `true`                   | Daily auto-update scheduler on/off     |
| `MEHL_DATA_UPDATE_HOUR` / `MEHL_DATA_UPDATE_MINUTE` | `17` / `30` (UTC) | Daily update time (default 23:00 IST, after bhavcopy + AMFI NAV publication) |

## Daily data updates

One shared routine (`backend/app/services/data_update.py`) pulls the latest data from every
source — bhavcopy, corporate actions, indices, and AMFI NAVs (skipping ISINs whose NAV is
< 7 days old) — then refreshes the Explore search coverage cache. Every ingest path is
idempotent, so re-running never duplicates rows. Two triggers call the same routine:

- **Scheduled**: APScheduler cron inside the FastAPI process, daily at
  `MEHL_DATA_UPDATE_HOUR:MEHL_DATA_UPDATE_MINUTE` UTC (default 17:30 UTC / 23:00 IST).
- **Manual**: the "Update data" button on the Dashboard, or
  `POST /api/data-update/run` (status: `GET /api/data-update/status`).
  A run that is already in progress is never started twice.

## API surface

- `GET /api/health` - liveness probe used by the compose healthcheck
- `GET /api/market/ohlcv/{symbol}?start=&end=&limit=&adjusted=` - OHLCV rows (DuckDB + Polars)
- `GET /api/market/indices/{symbol}?start=&end=` - NIFTY 50/500 daily closes
- `GET /api/market/tickers?q=&types=&limit=` - ticker search over the securities master + indices (symbol/name, type stock/etf/mf/index, first/last data dates)
- `GET /api/market/overview/{symbol}` - type-aware latest info: price/NAV, trailing returns (YTD/1Y/3Y/5Y/10Y), and key metrics for stocks/ETFs (PE, PB, ROE, ROCE, D/E, revenue/profit TTM, market cap)
- `GET /api/market/close-on?symbol=&day=` - close on the exact date, else nearest previous trading day (backs the transaction price auto-fill)
- `POST /api/market/screener` - rules-engine screener (`{filters: [{metric, op, value}], rank_by, rank_desc, limit, instrument_types}`) over fundamentals + latest snapshot; ops: `lt/lte/gt/gte/eq/between`
- `GET /api/market/fundamentals/{symbol}` - computed ratios (PE, PB, ROE, ROCE, D/E, margins, growth) + per-period history for trend charts
- `GET /api/market/fundamentals/{symbol}/statements?statement=` - raw statement line items per period
- `POST /api/market/backtest` - SMA-crossover backtest (`{symbol, fast, slow, initial_capital}`)
- `POST /api/market/backtest-allocation` - multi-asset allocation backtest (`{symbols, weights, start, initial_capital, monthly_contribution, annual_step_up_pct, rebalance_freq, benchmark}`) with XIRR/vol/Sharpe/max-drawdown/rolling-return metrics and index benchmark comparison
- `GET/POST /api/portfolios`, `GET/DELETE /api/portfolios/{id}` - portfolio CRUD
- `GET/POST /api/portfolios/{id}/transactions`, `DELETE .../transactions/{txn_id}` - transactions ledger (source of truth for holdings); price 0 is auto-filled from the close on the trade date (nearest previous trading day)
- `POST /api/portfolios/{id}/transactions/import` - CSV import (multipart); flexible column aliases (date, symbol, side, quantity, price, fees, note), dedupes exact repeats, returns `{imported, total, skipped: [{row, reason}]}`
- `GET /api/portfolios/{id}/positions` - holdings computed from the ledger (average-cost method)
- `GET /api/portfolios/{id}/summary` - holdings with live prices, unrealized/realized P&L, dividends, XIRR
- `GET /api/portfolios/{id}/gains` - realized capital gains via FIFO lots (LTCG/STCG + estimated tax)
- `GET /api/portfolios/{id}/equity-curve` - daily portfolio value + invested series
- `PUT /api/portfolios/{id}/rules` - target allocations + drift band
- `POST /api/portfolios/{id}/rebalance` - tax-aware rebalancing suggestions (buys preferred over taxable sells) + what-if projection
- `GET/POST /api/portfolios/{id}/dividends`, `DELETE .../dividends/{dividend_id}` - dividend records
- `GET/POST /api/screens`, `DELETE /api/screens/{id}` - saved screener filter sets
- `GET/POST /api/scenarios`, `PUT/DELETE /api/scenarios/{id}` - scenario CRUD (SIP, step-up, inflation, asset allocation)
- `POST /api/scenarios/preview`, `POST /api/scenarios/{id}/run` - deterministic projection + Monte Carlo percentile bands (p10/p50/p90)
- `POST /api/scenarios/goal` - required monthly SIP for a target corpus
- `GET/POST /api/watchlist`, `DELETE /api/watchlist/{id}` - watchlist CRUD
- `WS /ws` - WebSocket endpoint with a broadcast manager (echo for now)

## Project structure

```text
backend/
  app/
    api/routes/    # health, market, portfolio, ws
    engine/        # DuckDB access, screener, backtest (Polars)
    state/         # SQLAlchemy models, schemas, session
  data/            # market.duckdb, mehl.db (gitignored)
  tests/
frontend/
  src/
    api/           # typed fetch client + ws helper
    components/    # PriceChart (lightweight-charts), PositionsTable (TanStack)
    pages/         # Dashboard, Markets, Wealth, Scenarios, Backtest, Rebalancing
  nginx.conf       # reverse proxy /api + /ws -> backend:8000
docker-compose.yml
```

## Loading market data (Indian equities)

Scope: NSE mainboard daily OHLCV (series EQ/BE/BZ, SME excluded) from Jan 2000 to today, plus AMFI NAVs for MF/debt-fund schemes, quarterly/annual financial statements, and corporate actions (splits/bonuses). Raw as-traded prices are stored; adjustment for corporate actions happens at read time (`?adjusted=true`), so yfinance fills (`auto_adjust=False`) stay consistent with bhavcopy data.

### Sources

| Data | Source | Notes |
| --- | --- | --- |
| Instrument master | `archives.nseindia.com/content/equities/EQUITY_L.csv` | Symbol, ISIN, series, listing date; ETFs flagged by name |
| Daily OHLCV | NSE bhavcopy archives (`nsearchives.nseindia.com`) | UDiFF zip since Jul 2024, legacy zip before; both formats parsed |
| MF/debt fund NAVs | AMFI `NAVAll.txt` | Latest NAVs stored as daily rows (close = NAV, volume NULL, symbol = ISIN); scheme codes kept on `securities.amfi_code` |
| Historical MF NAVs | mfapi.in `/mf/{scheme}` | Full per-scheme NAV history via `amfi-history` (additive, never overwrites) |
| Benchmark indices | yfinance (`^NSEI`, `^CRSLDX`) | NIFTY 50 / NIFTY 500 daily closes into the `indices` table |
| Financial statements | yfinance (`get_income_stmt`/`get_balance_sheet`/`get_cashflow`, yearly+quarterly) | Normalized into the `financials` table |
| Corporate actions | NSE `corporates-corporateActions` API | Split/bonus ratios parsed from `subject`; dividends kept as `other` |
| Gap fill | yfinance raw history | Only inserts days missing in DuckDB, never overwrites |

All NSE/AMFI calls go through `RateLimitedSession` (`app/ingest/http.py`): min request interval (default 0.5s, tunable via `--rate`), exponential backoff, and cookie re-derivation on 401/403.

### CLI

```sh
cd backend
uv run python -m app.ingest.cli universe                          # instrument master
uv run python -m app.ingest.cli bhavcopy --start 2000-01-01       # ~6600 daily files, 1-2h
uv run python -m app.ingest.cli actions --start 2000-01-01        # corporate actions history
uv run python -m app.ingest.cli amfi                              # latest MF/debt fund NAVs
uv run python -m app.ingest.cli amfi-history                      # full NAV history per scheme via mfapi.in
uv run python -m app.ingest.cli indices                           # NIFTY 50/500 daily closes
uv run python -m app.ingest.cli financials --symbols RELIANCE,TCS # statements via yfinance
uv run python -m app.ingest.cli info --symbols RELIANCE,TCS       # sector/industry/shares via yfinance
uv run python -m app.ingest.cli fill-gaps --start 2000-01-01      # yfinance gap fill
uv run python -m app.ingest.cli import-zip                        # import an NSE dataset zip (SCRIP/+INDEX/ csvs), additive
uv run python -m app.ingest.cli update                            # daily update: bhavcopy + actions + indices + AMFI NAVs, idempotent
uv run python -m app.ingest.cli all                               # universe + bhavcopy + actions + amfi
```

`import-zip` reads Kaggle-style NSE dataset zips (`Datasets/SCRIP/*.csv` +
`Datasets/INDEX/*.csv`) and inserts additively - the 1990-2020 archive zip fills
the 2000-2019 block and index history (NIFTY 50/500/100/200, sector indices,
G-Sec yields, INDIA VIX) without touching NSE.

`import-bhavcopy-dir` reads a folder of daily `DDMMMYYYY.csv` bhavcopy files
(generated from github.com/girishg4t/bhavCopy-downloader) in any of its three
formats (legacy / nsepython-style / UDiFF) with merge semantics: the incoming
raw bhavcopy values win on (symbol, ts) conflicts, and days not in the folder
are preserved. A source-comparison audit found the zip's 2000-2001 OHLC/volume
data and some split-adjusted manual rows disagree with raw bhavcopy - the
folder import replaces those with the canonical raw prices.

All commands are idempotent - safe to re-run; bhavcopy upserts per day, actions/financials upsert on their natural keys.

### Transaction CSV format

Import on the Wealth page ("Import CSV") or `POST /api/portfolios/{id}/transactions/import`.
Columns (aliases accepted, case-insensitive):

```csv
Date,Symbol,Side,Quantity,Price,Fees,Note
2025-06-02,RELIANCE,buy,10,0,20.5,SIP installment
2025-06-10,NIFTYBEES,buy,100,285.4,0,
2025-06-20,RELIANCE,sell,5,298.1,15,
```

- `price` 0 or blank is auto-filled from the closing price on the trade date
  (nearest previous trading day); rows for symbols with no price data are skipped.
- Rows identical to an existing transaction (date + symbol + side + qty + price)
  are skipped as duplicates, so re-importing a file is safe.
- Response reports `{imported, total, skipped: [{row, reason}]}`.

### Price adjustment

`GET /api/market/ohlcv/{symbol}?adjusted=true` divides pre-ex-date prices by the stored factor (splits: old FV/new FV; bonus a:b: (a+b)/b), compounding across actions - implemented in `app/engine/adjust.py` on the raw table, so the raw as-traded record is preserved. yfinance-sourced data uses raw prices for the same reason.

### Known limitations

- Historical MF NAV backfill is additive via mfapi.in; run `amfi-mfapi` for a one-shot full backfill. The daily update service keeps NAVs current afterwards.
- Screener fundamentals require price data: ratios derived from the latest close (market cap, PE, PB) are nulled when the close is older than 120 days - complete the `bhavcopy` backfill and run `fill-gaps` before screening.
- yfinance occasionally reports isolated quarters in a wrong scale; the TTM engine detects unstable windows (max within 10x the median absolute value, 4+ quarters required) and falls back to the latest annual figure.
- Sector/industry come from yfinance `info`; run the `info` command to populate `securities_info`.
- Monte Carlo assumes zero correlation between assets (volatility blends as a weighted sum) and normal monthly returns; the median simulated path matches the deterministic projection at the same annual assumption.
- Capital gains use equity rates (post-Jul-2024): LTCG 12.5% above Rs 1.25L, STCG 20%; debt-fund taxation at slab rates is not modeled.
- Backtests ignore taxes on rebalancing trades and use month-end closes; volatility/Sharpe are computed from the contribution-inclusive value series (money-weighted XIRR is the headline return).
- Current data state: OHLCV fully loaded 1994-2026 (zip + bhavcopy folder + gap fills), AMFI NAVs via mfapi.in for 9,248 ISINs, indices, financials, corporate actions. Missing: recent NSE days on cloud IPs where nseindia.com is blocked (run `bhavcopy`/`update` from your own IP), and yfinance `info` for some symbols.
- NSE blocks some cloud IPs from `www.nseindia.com` API endpoints (corporate actions); run the `actions` command from the deployment machine or your own IP if it 403s.
- `nsepython` was evaluated but skipped: unmaintained and its endpoints break when NSE changes formats; the thin `RateLimitedSession` client does the same job with visible rate limiting.
- Bonus/split parsing covers common NSE `subject` formats; unusual wordings fall back to `other` (no factor, no price adjustment).
