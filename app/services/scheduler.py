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
from app.connectors.pimland_ecom_sync import run_ecom_sync
from scripts.sync_product_malzeme import run as run_malzeme_sync

log = get_logger(__name__)

_scheduler = AsyncIOScheduler()


def _make_job(source_id: str):
    async def _job():
        log.info("scheduler.job_started", source=source_id)
        result = await sync_run(source_id)
        log.info("scheduler.job_done", source=source_id, status=result.status)
    return _job


async def _search_index_job():
    """Her gece 04:00 — PLM sync'ten sonra arama indexini güncelle."""
    from app.services.enrichment.product_indexer import run_indexer
    log.info("scheduler.search_index_started")
    try:
        result = await run_indexer()
        log.info("scheduler.search_index_done", **result)
    except Exception as exc:
        log.error("scheduler.search_index_failed", error=str(exc))


async def _ecom_sync_job():
    """Her gece 02:00 — ecom katalog senkronizasyonu (katalog sync sonrası)."""
    log.info("scheduler.ecom_sync_started")
    try:
        result = await run_ecom_sync()
        log.info("scheduler.ecom_sync_done", **result)
    except Exception as exc:
        log.error("scheduler.ecom_sync_failed", error=str(exc))


async def _malzeme_sync_job():
    """Her gece 03:30 — ürün malzeme detayları (PLM sync sonrası)."""
    log.info("scheduler.malzeme_sync_started")
    try:
        result = await asyncio.to_thread(run_malzeme_sync)
        log.info("scheduler.malzeme_sync_done", **result)
    except Exception as exc:
        log.error("scheduler.malzeme_sync_failed", error=str(exc))


async def _generate_due_briefs_job():
    """Her 30dk'da bir — bugün için üretilmemiş aktif brifleri üret."""
    from datetime import date
    from app.db.session import AsyncSessionLocal
    from sqlalchemy import text
    from app.services.daily_brief.orchestrator import generate_brief

    log.info("scheduler.brief_generation_started")
    generated = 0
    try:
        async with AsyncSessionLocal() as session:
            due_rows = (await session.execute(text("""
                SELECT s.id AS schedule_id
                FROM brief_schedules s
                JOIN brief_profiles p ON p.id = s.profile_id
                WHERE s.is_active = true
                  AND p.is_active = true
                  AND s.frequency_type = 'daily'
                  AND EXTRACT(DOW FROM NOW()::date) = ANY(
                      ARRAY(SELECT jsonb_array_elements_text(s.active_days)::int)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM brief_history h
                      WHERE h.schedule_id = s.id
                        AND h.brief_date = CURRENT_DATE
                  )
                  AND s.schedule_time <= CURRENT_TIME
            """))).mappings().all()

            for row in due_rows:
                try:
                    result = await generate_brief(row["schedule_id"], session)
                    if "hata" not in result:
                        generated += 1
                        log.info("scheduler.brief_generated", schedule_id=row["schedule_id"])
                except Exception as exc:
                    log.error("scheduler.brief_generate_failed", schedule_id=row["schedule_id"], error=str(exc))
    except Exception as exc:
        log.error("scheduler.brief_generation_failed", error=str(exc))
    log.info("scheduler.brief_generation_done", generated=generated)


async def _premium_brief_job() -> None:
    """Her gece 03:00 UTC — EC + MG Premium Brief v2 üretimi."""
    log.info("scheduler.premium_brief_started")
    try:
        from handlers.brief_daily import run_brief_daily
        result = await run_brief_daily()
        log.info(
            "scheduler.premium_brief_done",
            ec_status=result.get("ec", {}).get("status"),
            mg_status=result.get("mg", {}).get("status"),
            elapsed_ms=result.get("elapsed_ms"),
        )
    except Exception as exc:
        log.error("scheduler.premium_brief_failed", error=str(exc))


def register_jobs() -> None:
    """Register a scheduler job for every source that has a schedule config."""
    # Arama index — nightly 04:00 (PLM sync 03:00'den sonra)
    _scheduler.add_job(
        _search_index_job,
        CronTrigger.from_crontab("0 4 * * *"),
        id="search_index",
        replace_existing=True,
        misfire_grace_time=600,
    )
    log.info("scheduler.job_registered", source="search_index", trigger="CronTrigger")

    # Ecom katalog senkronizasyonu — 02:00 (pimland_ecom_catalogs 01:30 sonrası)
    _scheduler.add_job(
        _ecom_sync_job,
        CronTrigger.from_crontab("0 2 * * *"),
        id="ecom_sync",
        replace_existing=True,
        misfire_grace_time=600,
    )
    log.info("scheduler.job_registered", source="ecom_sync", trigger="CronTrigger")

    # Ürün malzeme detayları — 03:30 (PLM ürün sync 03:00 sonrası)
    _scheduler.add_job(
        _malzeme_sync_job,
        CronTrigger.from_crontab("30 3 * * *"),
        id="malzeme_sync",
        replace_existing=True,
        misfire_grace_time=600,
    )
    log.info("scheduler.job_registered", source="malzeme_sync", trigger="CronTrigger")


    # Brief üretim kontrolü — her 30dk çalışır, zamanı gelen briefleri üretir
    _scheduler.add_job(
        _generate_due_briefs_job,
        CronTrigger.from_crontab("*/30 5-12 * * 1-5"),
        id="brief_generation",
        replace_existing=True,
        misfire_grace_time=300,
    )
    log.info("scheduler.job_registered", source="brief_generation", trigger="CronTrigger")

    # adL Premium Brief v2 — EC + MG günlük üretim (03:00 UTC = 06:00 İstanbul)
    _scheduler.add_job(
        _premium_brief_job,
        CronTrigger.from_crontab("0 3 * * *"),
        id="premium_brief_daily",
        replace_existing=True,
        misfire_grace_time=1800,   # 30dk pencere — gece sync gecikmelerine karşı
    )
    log.info("scheduler.job_registered", source="premium_brief_daily", trigger="CronTrigger")

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
