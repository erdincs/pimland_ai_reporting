"""Sync orkestratör — tüm job'ları koordine eder.

Config kaynağı: sync/config/sources.yaml
Her job sync_log tablosuna kaydedilir.
Hata durumunda 3 dakika bekleyip 2 kez daha denenir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from sync.config_loader import (
    get_incorta_tables,
    get_incorta_auth,
    get_pimland_master_tables,
    get_pimland_products_config,
    get_pimland_credentials,
    load_views,
)
from sync.sources.incorta_sync import generic_incorta_sync
from sync.sources.pimland_sync import sync_master_table, sync_products
from sync.jobs.refresh_views import refresh_all_views
from sync.jobs.validate import run_validation
from sync.jobs.daily_report import send_daily_report

log = logging.getLogger(__name__)

_RETRY_WAIT = 180
_MAX_RETRY  = 2


# ── DB bağlantısı ─────────────────────────────────────────────────────────────

def _get_db_conn() -> "psycopg2.connection":
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "pimland_reporting"),
        user=os.environ.get("DB_USER", "pimland"),
        password=os.environ.get("DB_PASSWORD", "change_me"),
        connect_timeout=10,
    )


# ── sync_log ──────────────────────────────────────────────────────────────────

_CREATE_SYNC_LOG = """
CREATE TABLE IF NOT EXISTS sync_log (
    id           SERIAL PRIMARY KEY,
    job_name     VARCHAR(50),
    started_at   TIMESTAMP DEFAULT NOW(),
    finished_at  TIMESTAMP,
    status       VARCHAR(20),
    kayit_sayisi INTEGER,
    hata_mesaji  TEXT,
    detay        JSONB
);
"""


def _log_start(conn, job_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_log (job_name, started_at, status) VALUES (%s, NOW(), 'running') RETURNING id",
            (job_name,),
        )
        log_id = cur.fetchone()[0]
    conn.commit()
    return log_id


def _log_finish(conn, log_id: int, status: str,
                kayit_sayisi: int = 0, hata: str = None, detay: dict = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE sync_log SET finished_at=NOW(), status=%s,
               kayit_sayisi=%s, hata_mesaji=%s, detay=%s WHERE id=%s""",
            (status, kayit_sayisi, hata,
             json.dumps(detay, ensure_ascii=False, default=str) if detay else None,
             log_id),
        )
    conn.commit()


def _last_success_date(conn, job_name: str) -> Optional[datetime]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT finished_at FROM sync_log WHERE job_name=%s AND status='success' ORDER BY finished_at DESC LIMIT 1",
            (job_name,),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ── Generic job fonksiyonları ─────────────────────────────────────────────────

def _run_incorta_sync(conn) -> Dict[str, Any]:
    """sources.yaml'daki tüm Incorta tablolarını config'e göre sync eder."""
    token = get_incorta_auth()
    if not token or token.startswith("${"):
        raise ValueError("INCORTA_TOKEN env var eksik")

    results = []
    for table_cfg in get_incorta_tables():
        result = generic_incorta_sync(conn, table_cfg, token)
        results.append(result)

    total = sum(r["eklenen"] for r in results)
    return {"tablolar": results, "toplam_eklenen": total}


def _run_pimland_sync(conn) -> Dict[str, Any]:
    """Günlük delta — sadece pim_products."""
    creds = get_pimland_credentials()
    last_sync = _last_success_date(conn, "pimland_sync")
    result = sync_products(conn, last_sync_date=last_sync)
    return result


def _run_master_data(conn) -> Dict[str, Any]:
    """Haftalık full replace — tüm master data tabloları."""
    creds = get_pimland_credentials()
    results = []
    for table_cfg in get_pimland_master_tables():
        result = sync_master_table(conn, table_cfg, creds)
        results.append(result)
    total = sum(r["eklenen"] for r in results)
    return {"tablolar": results, "toplam_eklenen": total}


def _run_view_refresh(conn) -> Dict[str, Any]:
    """views.yaml'daki sırayla tüm view'ları refresh eder."""
    return refresh_all_views(conn)


def _run_validation(conn) -> Dict[str, Any]:
    return run_validation(conn)


_JOBS = {
    "incorta_sync":  _run_incorta_sync,
    "pimland_sync":  _run_pimland_sync,
    "master_data":   _run_master_data,
    "view_refresh":  _run_view_refresh,
    "validation":    _run_validation,
    "daily_report":  lambda conn: send_daily_report(conn),
}


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────

def run_job(job_name: str, dry_run: bool = False) -> Dict[str, Any]:
    if job_name not in _JOBS:
        return {"error": f"Bilinmeyen job: '{job_name}'. Geçerli: {list(_JOBS)}"}

    log.info("run_job başlıyor job=%s dry_run=%s", job_name, dry_run)

    conn = _get_db_conn()
    with conn.cursor() as cur:
        cur.execute(_CREATE_SYNC_LOG)
    conn.commit()

    if dry_run:
        log.info("[DRY_RUN] %s — gerçek işlem yapılmıyor", job_name)
        conn.close()
        return {"job": job_name, "dry_run": True, "status": "skipped"}

    log_id = _log_start(conn, job_name)
    attempt = 0
    last_error = None

    while attempt <= _MAX_RETRY:
        if attempt > 0:
            log.warning("retry %d/%d job=%s bekle=%ds", attempt, _MAX_RETRY, job_name, _RETRY_WAIT)
            time.sleep(_RETRY_WAIT)

        try:
            result = _JOBS[job_name](conn)
            kayit = (
                result.get("toplam_eklenen")
                or result.get("eklenen")
                or result.get("toplam_kontrol")
                or 0
            )
            status = "partial" if result.get("hatali") else "success"
            _log_finish(conn, log_id, status, kayit_sayisi=kayit, detay=result)
            log.info("run_job bitti job=%s status=%s kayit=%d", job_name, status, kayit)
            conn.close()
            return {"job": job_name, "status": status, "attempt": attempt + 1, **result}

        except Exception as exc:
            last_error = str(exc)
            log.error("run_job hata job=%s attempt=%d hata=%s", job_name, attempt + 1, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            attempt += 1

    _log_finish(conn, log_id, "failed", hata=last_error)
    log.error("run_job FAILED job=%s hata=%s", job_name, last_error)
    conn.close()
    return {"job": job_name, "status": "failed", "hata": last_error}
