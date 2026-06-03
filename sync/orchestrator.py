"""Sync orkestratör — tüm job'ları koordine eder.

Her job sync_log tablosuna kaydedilir.
Hata durumunda 3 dakika bekleyip 2 kez daha denenir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras

from sync.sources.incorta_sync import (
    sync_incorta_analytics,
    sync_incorta_depo_iade,
    sync_incorta_iptal_siparis,
    sync_incorta_satis,
)
from sync.sources.pimland_sync import sync_master_data, sync_products
from sync.jobs.refresh_views import refresh_all_views
from sync.jobs.validate import run_validation

log = logging.getLogger(__name__)

_RETRY_WAIT = 180   # saniye (3 dakika)
_MAX_RETRY  = 2     # ek deneme sayısı


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


def _log_start(conn: "psycopg2.connection", job_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_log (job_name, started_at, status) VALUES (%s, NOW(), 'running') RETURNING id",
            (job_name,),
        )
        log_id = cur.fetchone()[0]
    conn.commit()
    return log_id


def _log_finish(
    conn: "psycopg2.connection",
    log_id: int,
    status: str,
    kayit_sayisi: int = 0,
    hata: Optional[str] = None,
    detay: Optional[Dict] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE sync_log SET
               finished_at  = NOW(),
               status       = %s,
               kayit_sayisi = %s,
               hata_mesaji  = %s,
               detay        = %s
               WHERE id = %s""",
            (status, kayit_sayisi, hata,
             json.dumps(detay, ensure_ascii=False, default=str) if detay else None,
             log_id),
        )
    conn.commit()


def _last_success_date(conn: "psycopg2.connection", job_name: str) -> Optional[datetime]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT finished_at FROM sync_log WHERE job_name=%s AND status='success' ORDER BY finished_at DESC LIMIT 1",
            (job_name,),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ── Job tanımları ─────────────────────────────────────────────────────────────

def _run_incorta_sync(conn: "psycopg2.connection") -> Dict[str, Any]:
    token = os.environ.get("INCORTA_TOKEN", "")
    if not token:
        raise ValueError("INCORTA_TOKEN env var eksik")
    results = [
        sync_incorta_satis(conn, token),
        sync_incorta_depo_iade(conn, token),
        sync_incorta_iptal_siparis(conn, token),
        sync_incorta_analytics(conn, token),
    ]
    total = sum(r["eklenen"] for r in results)
    return {"tablolar": results, "toplam_eklenen": total}


def _run_pimland_sync(conn: "psycopg2.connection") -> Dict[str, Any]:
    last_sync = _last_success_date(conn, "pimland_sync")
    result = sync_products(conn, last_sync_date=last_sync)
    return result


def _run_master_data(conn: "psycopg2.connection") -> Dict[str, Any]:
    results = sync_master_data(conn)
    total = sum(r["eklenen"] for r in results)
    return {"tablolar": results, "toplam_eklenen": total}


def _run_view_refresh(conn: "psycopg2.connection") -> Dict[str, Any]:
    return refresh_all_views(conn)


def _run_validation(conn: "psycopg2.connection") -> Dict[str, Any]:
    return run_validation(conn)


_JOBS = {
    "incorta_sync": _run_incorta_sync,
    "pimland_sync": _run_pimland_sync,
    "master_data":  _run_master_data,
    "view_refresh": _run_view_refresh,
    "validation":   _run_validation,
}


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────

def run_job(job_name: str, dry_run: bool = False) -> Dict[str, Any]:
    """Job çalıştır, retry uygula, sync_log'a yaz."""
    if job_name not in _JOBS:
        return {"error": f"Bilinmeyen job: {job_name}. Geçerli: {list(_JOBS)}"}

    log.info("run_job başlıyor job=%s dry_run=%s", job_name, dry_run)

    conn = _get_db_conn()

    # sync_log tablosunu oluştur (ilk çalışmada)
    with conn.cursor() as cur:
        cur.execute(_CREATE_SYNC_LOG)
    conn.commit()

    if dry_run:
        log.info("[DRY_RUN] %s — gerçek işlem yapılmıyor", job_name)
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

    # 3 denemede de başarısız
    _log_finish(conn, log_id, "failed", hata_mesaji=last_error)
    log.error("run_job FAILED job=%s hata=%s", job_name, last_error)
    conn.close()
    return {"job": job_name, "status": "failed", "hata": last_error}
