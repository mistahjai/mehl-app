import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import data_update, health, market, portfolio, screens, scenarios, ws
from app.config import settings
from app.services import data_update as data_update_service
from app.state.db import init_db

logger = logging.getLogger(__name__)


async def run_data_update_job() -> None:
    """Named async function for the scheduler to avoid lambda capture issues."""
    await asyncio.to_thread(data_update_service.run_data_update, trigger="scheduled")


async def scheduler_watchdog(scheduler: AsyncIOScheduler) -> None:
    """Periodically check if scheduled run was missed and trigger catch-up."""
    while True:
        await asyncio.sleep(3600)  # 1 hour
        job = scheduler.get_job("data_update")
        if job and job.next_run_time:
            overdue = (datetime.now(UTC) - job.next_run_time).total_seconds()
            if overdue > 10800:  # 3 hours
                logger.info("Scheduled run overdue by %.0fs, triggering watchdog catch-up", overdue)
                asyncio.create_task(asyncio.to_thread(data_update_service.run_data_update, trigger="watchdog"))


# String reference for SQLAlchemyJobStore serialization
RUN_DATA_UPDATE_JOB_REF = "app.main:run_data_update_job"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    scheduler: AsyncIOScheduler | None = None
    if settings.data_update_enabled:
        jobstore_path = settings.data_update_jobstore_path
        jobstore_path.parent.mkdir(parents=True, exist_ok=True)
        jobstore_url = f"sqlite:///{jobstore_path}"
        logger.info("Scheduler starting, job store: %s", jobstore_url)
        scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
            timezone="UTC",
        )
        scheduler.add_job(
            RUN_DATA_UPDATE_JOB_REF,
            CronTrigger(
                hour=settings.data_update_hour,
                minute=settings.data_update_minute,
                timezone="UTC",
            ),
            id="data_update",
            # APScheduler defaults misfire_grace_time to 1 second, so a job that
            # could not start within a second of its trigger was silently dropped
            # and the day was simply missed. The update takes minutes, so a busy
            # event loop or a slow start is normal rather than exceptional.
            misfire_grace_time=3600,
            # One miss is one run -- do not replay every trigger that elapsed.
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        scheduler.start()
        job = scheduler.get_job("data_update")
        logger.info("Data update job registered, next run: %s", job.next_run_time)
        logger.info("Scheduler started")
        # Expose scheduler for health endpoint
        app.state.scheduler = scheduler
        # The cron job only exists while this process does, so a deploy, a crash
        # or a container stop across the trigger time loses the day outright.
        # Catch up once at startup if the last successful run is stale.
        if (
            settings.data_update_catchup_enabled
            and data_update_service.needs_catchup()
        ):
            logger.warning("last data update is stale; running catch-up now")
            asyncio.create_task(asyncio.to_thread(data_update_service.run_data_update, trigger="catchup"))
        # Start watchdog to detect missed runs
        asyncio.create_task(scheduler_watchdog(scheduler))
    yield
    if scheduler is not None:
        logger.info("Scheduler shutting down")
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(market.router)
app.include_router(portfolio.router)
app.include_router(portfolio.watchlist_router)
app.include_router(screens.router)
app.include_router(scenarios.router)
app.include_router(data_update.router)
app.include_router(ws.router)
