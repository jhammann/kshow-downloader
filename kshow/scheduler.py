import asyncio
import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from kshow.db import get_setting
from kshow.downloads import process_downloads
from kshow.pipeline import Deps, check_all_shows, check_show

log = logging.getLogger(__name__)


def _guarded(
    name: str, lock: asyncio.Lock, fn: Callable[[], Awaitable[None]]
) -> Callable[[], Awaitable[None]]:
    """Run fn under lock; log and swallow any exception so the scheduler survives."""

    async def run() -> None:
        try:
            async with lock:
                await fn()
        except Exception:
            log.exception("Scheduled job %s failed", name)

    return run


def start_scheduler(
    deps: Deps,
    check_lock: asyncio.Lock | None = None,
    downloads_lock: asyncio.Lock | None = None,
) -> AsyncIOScheduler:
    check_lock = check_lock or asyncio.Lock()
    downloads_lock = downloads_lock or asyncio.Lock()
    with deps.session_factory() as session:
        minutes = int(get_setting(session, "check_interval_minutes"))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _guarded("check", check_lock, lambda: check_all_shows(deps)),
        "interval",
        minutes=minutes,
        id="check",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guarded("downloads", downloads_lock, lambda: process_downloads(deps)),
        "interval",
        seconds=60,
        id="downloads",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler


def reschedule_check(scheduler: AsyncIOScheduler, minutes: int) -> None:
    scheduler.reschedule_job("check", trigger="interval", minutes=minutes)


def trigger_check(app, show_id: int | None) -> None:
    """Run an immediate one-off check (one show, or all) under the shared check lock."""
    deps: Deps = app.state.deps

    async def run() -> None:
        if show_id is None:
            await check_all_shows(deps)
        else:
            await check_show(deps, show_id)

    task = asyncio.get_running_loop().create_task(
        _guarded("manual check", app.state.check_lock, run)()
    )
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
