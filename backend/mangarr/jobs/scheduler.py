import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..db import session_scope
from .. import settings_service
from .tasks import monitor_all, process_direct_queue, recover_interrupted_downloads, sync_qbittorrent

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

DEFAULT_MONITOR_MINUTES = 60


async def start() -> None:
    async with session_scope() as session:
        raw = await settings_service.get(session, "monitor_interval_minutes")
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
    scheduler.start()
    log.info("Scheduler started (monitor every %d min)", interval)


def reschedule_monitor(minutes: int) -> None:
    """Apply a new monitor interval without restarting the app."""
    if scheduler.running and scheduler.get_job("monitor"):
        scheduler.reschedule_job("monitor", trigger="interval", minutes=max(1, minutes))
        log.info("Monitor rescheduled to every %d min", minutes)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
