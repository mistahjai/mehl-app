# Memory

## Degenerate output loops
Two assistant replies degenerated into thousands of repeated "lock" tokens, usually right after large tool outputs, producing no work. Keep final replies very short; never re-emit large tool outputs, always summarize. If a turn seems to stall, check git/file state before redoing work.

## upsert_ohlcv replaces symbol rows
`market_db.upsert_ohlcv` deletes ALL existing rows for the symbols in the batch before inserting — a second call with different days wipes earlier days. For partial-day batches use `merge_ohlcv` (preserves days not in the batch). Bit the test seeding in test_api.py.

## DuckDB stale lock (PID 0) after SIGKILL
Killing a process holding market.duckdb open can leave "Conflicting lock is held in PID 0" even with no /proc/locks entry and no fd holder (overlayfs quirk). Fix: copy the file to a new inode (`cp` to /tmp then `mv` back) — clears the lock, data intact.

## AMFI host split: www blocked, portal works
`www.amfiindia.com` resolves to 14.143.46.156 and drops TCP on 80/443 from this host (Google DNS agrees, so it is not a resolver artifact), while `portal.amfiindia.com` -> 14.143.46.157 answers normally with current data. Do not conclude "AMFI is unreachable" from a NAVAll.txt timeout — test the portal host separately. `api.amfiindia.com` is reachable but serves a snapshot frozen at 2022-01-23.

## AMFI portal returns HTTP 200 with an error page
`DownloadNAVHistoryReport_Po.aspx` caps the requested date window and, when it is too wide, answers **200 OK** with a ~13,703-byte HTML form containing "Application Error!" — no non-200 status, no exception. Measured 2026-09-27: 11 days reliable, 30 days failed 1 run in 3, 47+ days failed every time. Always validate the body (marker + expected header) and chunk small; a status-code check lets a backfill "succeed" while writing nothing.

## mfapi.in /mf/{code} history is complete, not frozen
Earlier I concluded the per-scheme mfapi history endpoint was frozen at 2025-12-31 and that a 46-minute backfill producing 0 rows was a failure. Both were wrong: the endpoint returns full per-scheme history (119598: 3,389 rows back to 2013), each scheme's newest date simply being when that scheme last published, and the 0-row backfill was a correct no-op because the data was already there. Verified our stored history matches mfapi exactly (0 missing / 0 extra / 0 NAV mismatches). Verify before blaming a source for returning old data.

## Ladder rungs must gate on "ok", not on row count
`ingest_nav_latest` originally advanced to the next source when a rung returned 0 rows. On an already-current database that is a *correct* no-op, so every daily run fell through to the 9,248-request per-scheme fallback. Each rung now returns an explicit `"ok"` and the ladder gates on that. Any future rung needs the same flag.

## Backfilling historical NAVs cannot reuse the daily ingest path
`_drop_regressions` keeps only rows *past* each symbol's high-water mark, which is correct for daily runs and exactly wrong for a backfill: a hole in the middle of a history is older than the newest row already stored, so the guard would discard the very rows you are trying to add. Routing a historical frame through `ingest_portal_history` reports `ok=True` while writing almost nothing. `nav_audit.backfill_missing` therefore selects absent (symbol, ts) keys itself and writes them via `insert_ohlcv` (`ON CONFLICT DO NOTHING`), which also means a backfill can never restate a value or delete a row -- those stay deliberate, separate decisions.

## Never compare published floats with a bare epsilon
AMFI NAVs are published to 4 dp, so a one-unit change in the last digit *is* exactly 1e-4. `abs(a - b) > 1e-4` sits precisely on that boundary: `12.6765-12.6764` landed under the threshold while `1021.4097-1021.4096` landed over it, so 117 of 2,092 reported "mismatches" were pure rounding wobble. Compare in whole published units instead -- `((a-b).abs() * 10**4).round() > 1`. Real restatements are orders of magnitude larger (0.02% is 100+ units).
