import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import data_update, health, market, portfolio, screens, scenarios, ws
from app.config import settings
from app.services import data_update as data_update_service
from app.state.db import init_db

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    scheduler: AsyncIOScheduler | None = None
    if settings.data_update_enabled:
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            lambda: asyncio.to_thread(data_update_service.run_data_update),
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
        )
        scheduler.start()
        # The cron job only exists while this process does, so a deploy, a crash
        # or a container stop across the trigger time loses the day outright.
        # Catch up once at startup if the last successful run is stale.
        if (
            settings.data_update_catchup_enabled
            and data_update_service.needs_catchup()
        ):
            logger.warning("last data update is stale; running catch-up now")
            asyncio.create_task(asyncio.to_thread(data_update_service.run_data_update))
    yield
    if scheduler is not None:
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
