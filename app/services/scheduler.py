"""APScheduler wrapper for periodic connector syncs.

Started and stopped via the FastAPI lifespan. Each source with a `schedule`
block in its YAML gets a job registered here at startup.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.connectors.registry import registry
from app.connectors.sync_pipeline import run as sync_run
from app.core.logging import get_logger

log = get_logger(__name__)

_scheduler = AsyncIOScheduler()


def _make_job(source_id: str):
    async def _job():
        log.info("scheduler.job_started", source=source_id)
        result = await sync_run(source_id)
        log.info("scheduler.job_done", source=source_id, status=result.status)
    return _job


def register_jobs() -> None:
    """Register a scheduler job for every source that has a schedule config."""
    for source_id, cfg in registry.all_configs().items():
        if not cfg.schedule:
            continue
        sched = cfg.schedule
        if sched.cron:
            trigger = CronTrigger.from_crontab(sched.cron)
        elif sched.interval_minutes:
            trigger = IntervalTrigger(minutes=sched.interval_minutes)
        else:
            continue

        _scheduler.add_job(
            _make_job(source_id),
            trigger=trigger,
            id=f"sync_{source_id}",
            replace_existing=True,
            misfire_grace_time=300,
        )
        log.info("scheduler.job_registered", source=source_id,
                 trigger=trigger.__class__.__name__)


def start() -> None:
    register_jobs()
    _scheduler.start()
    log.info("scheduler.started", jobs=len(_scheduler.get_jobs()))


def stop() -> None:
    _scheduler.shutdown(wait=False)
    log.info("scheduler.stopped")
