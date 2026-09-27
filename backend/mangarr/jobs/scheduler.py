import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..db import session_scope
from .. import settings_service
from ..import_lists import sync_all_lists
from .tasks import monitor_all, process_direct_queue, recover_interrupted_downloads, sync_qbittorrent

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

DEFAULT_MONITOR_MINUTES = 60
DEFAULT_IMPORT_LIST_HOURS = 6


async def start() -> None:
    async with session_scope() as session:
        raw = await settings_service.get(session, "monitor_interval_minutes")
        raw_lists = await settings_service.get(session, "import_list_sync_hours")
    try:
        list_hours = max(1, int(raw_lists))
    except (TypeError, ValueError):
        log.warning("invalid import_list_sync_hours %r; using %d",
                    raw_lists, DEFAULT_IMPORT_LIST_HOURS)
        list_hours = DEFAULT_IMPORT_LIST_HOURS
    try:
        interval = max(1, int(raw))
    except (TypeError, ValueError):
        # a bad stored value must not prevent startup
        log.warning("invalid monitor_interval_minutes %r; using %d", raw, DEFAULT_MONITOR_MINUTES)
        interval = DEFAULT_MONITOR_MINUTES

    # downloads interrupted by the previous shutdown would otherwise be
    # stranded in a state the queue worker never picks up
    await recover_interrupted_downloads()

    scheduler.add_job(
        process_direct_queue, "interval", seconds=10,
        id="direct_queue", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        sync_qbittorrent, "interval", seconds=8,
        id="qbt_sync", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        monitor_all, "interval", minutes=interval,
        id="monitor", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        sync_all_lists, "interval", hours=list_hours,
        id="import_lists", max_instances=1, coalesce=True,
    )
    scheduler.start()
    log.info("Scheduler started (monitor every %d min)", interval)


def reschedule_monitor(minutes: int) -> None:
    """Apply a new monitor interval without restarting the app."""
    if scheduler.running and scheduler.get_job("monitor"):
        scheduler.reschedule_job("monitor", trigger="interval", minutes=max(1, minutes))
        log.info("Monitor rescheduled to every %d min", minutes)


def reschedule_import_lists(hours: int) -> None:
    if scheduler.running and scheduler.get_job("import_lists"):
        scheduler.reschedule_job("import_lists", trigger="interval", hours=max(1, hours))
        log.info("Import lists rescheduled to every %d h", hours)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
