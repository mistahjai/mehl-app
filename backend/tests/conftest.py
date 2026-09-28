import os
import tempfile

os.environ.setdefault("MEHL_DATA_DIR", tempfile.mkdtemp(prefix="mehl-test-"))
# Starting the app schedules a catch-up update when the last run is stale, which
# in a fresh temp database is always. Left on, merely booting the test client
# kicks off a real network ingest and pollutes the shared temp market.duckdb.
os.environ.setdefault("MEHL_DATA_UPDATE_CATCHUP_ENABLED", "false")
