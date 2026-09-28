import asyncio

from fastapi import APIRouter

from app.services import data_update

router = APIRouter(prefix="/api/data-update", tags=["data-update"])

_background_tasks: set[asyncio.Task] = set()


@router.get("/status")
async def get_update_status() -> dict:
    return {"running": data_update.is_running(), "last_run": data_update.read_status()}


@router.post("/run")
async def trigger_update() -> dict:
    """Manual trigger: runs the same update routine as the scheduled job."""
    if data_update.is_running():
        return {"status": "already_running"}
    task = asyncio.create_task(asyncio.to_thread(data_update.run_data_update))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "started"}
