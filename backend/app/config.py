from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MEHL_", extra="ignore")

    app_name: str = "mehl-api"
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    database_url: str | None = None
    cors_origins: list[str] = ["http://localhost:5173"]
    data_update_enabled: bool = True
    data_update_hour: int = 17
    data_update_minute: int = 30
    # The cron job only exists while this process runs, so a restart across the
    # trigger time would otherwise skip the day. Turn this off where starting the
    # app must not kick off a real ingest (tests, one-off tooling).
    data_update_catchup_enabled: bool = True
    # Path to the APScheduler job store (SQLite). Survives container restarts.
    data_update_jobstore_path: Path = Path(__file__).resolve().parent.parent / "data" / "scheduler.sqlite"

    # Financials data source configuration
    financials_primary_source: str = "screener"  # "screener" | "yfinance"
    financials_consolidation: str = "consolidated"  # "consolidated" | "standalone"
    financials_fallback_source: str = "yfinance"  # "yfinance" | "none"

    @property
    def effective_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'mehl.db'}"

    @property
    def market_db_path(self) -> Path:
        return self.data_dir / "market.duckdb"


settings = Settings()
