import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from app.services import data_update

router = APIRouter(prefix="/api/data-update", tags=["data-update"])

_background_tasks: set[asyncio.Task] = set()


@router.get("/status")
async def get_update_status() -> dict:
    return {"running": data_update.is_running(), "last_run": data_update.read_status()}


@router.get("/scheduler-status")
async def get_scheduler_status(request: Request) -> dict:
    """Get scheduler health and next run info."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return {"running": False, "job_registered": False}
    job = scheduler.get_job("data_update")
    if job is None:
        return {"running": scheduler.running, "job_registered": False}
    next_run = job.next_run_time
    overdue_seconds = None
    if next_run:
        overdue_seconds = max(0, (datetime.now(UTC) - next_run).total_seconds())
    last_run = data_update.read_status()
    return {
        "running": scheduler.running,
        "job_registered": True,
        "next_run_time": next_run.isoformat() if next_run else None,
        "overdue_seconds": overdue_seconds,
        "last_run_status": last_run.get("status"),
        "last_run_at": last_run.get("last_run_at"),
        "last_trigger": last_run.get("trigger"),
    }


@router.post("/run")
async def trigger_update() -> dict:
    """Manual trigger: runs the same update routine as the scheduled job."""
    if data_update.is_running():
        return {"status": "already_running"}
    task = asyncio.create_task(asyncio.to_thread(data_update.run_data_update, trigger="manual"))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "started"}
