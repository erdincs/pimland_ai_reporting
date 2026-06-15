"""Sync pipeline: connector.fetch() → normalise → bulk load → refresh views.

Each run is recorded as a SyncJob row so history and failures are auditable.
Runs are idempotent by default (if_exists='replace' — configurable per source).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, date, timezone, timedelta
from typing import Any, Dict, List, Optional, Set

import pandas as pd
from sqlalchemy import create_engine, text

from app.connectors.registry import registry
from app.core.config import settings
from app.core.exceptions import IngestionError
from app.core.logging import get_logger
from app.ingestion.normalizer import normalise_records
from app.schemas.connector import SyncJobResult

log = get_logger(__name__)

# Sync pipeline uses the sync (psycopg2) engine — same pattern as excel_loader.
_sync_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)

# Materialized views that should be refreshed after every sync.
# Only views whose source tables were updated are refreshed.
_VIEW_DEPENDENCIES: dict = {
    "eticaret_satis":      ["mv_satis_aylik", "mv_satis_urun", "mv_satis_kanal"],
    "pim_products":        ["mv_ecom_product_placement", "mv_product_malzeme_ozet"],
    "pim_ecom_catalogs":   [],  # ecom_sync job view'ları kendisi yeniler
    "pim_product_malzeme": ["mv_product_malzeme_ozet"],
}

# Tablolar için beklenen minimum yıl — yeni veri daha eskiyse eski veriyi koru.
# Bu sayede Incorta geçici olarak eski veri döndürdüğünde DB silinmez.
_MIN_YEAR_GUARD: dict = {
    "incorta_magaza_performans": 2025,  # 2025+ yılı yoksa replace etme
}


async def run(
    source_id: str,
    if_exists: str = "replace",
    job_id: Optional[str] = None,
) -> SyncJobResult:
    """Run a full or incremental sync for `source_id`. Returns a SyncJobResult."""
    job_id = job_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    _write_job(job_id, source_id, "running", started_at)

    try:
        connector = registry.get(source_id)
        cfg = connector.config

        # ── Incremental mode detection ──────────────────────────────────────
        incr = getattr(cfg.tool, "incremental", None) if cfg.tool else None
        extra_prompts = None
        is_incremental = False

        if incr and if_exists == "replace":
            last_date = _get_last_date(cfg.target_table, incr.date_field)
            if last_date is not None:
                is_incremental = True
                if_exists = "append"   # will use delete+insert below
                today_str  = date.today().strftime(incr.date_format)
                last_str   = last_date.strftime(incr.date_format)
                extra_prompts = [{
                    "field":    incr.incorta_date_field,
                    "operator": "BETWEEN",
                    "values":   [last_str, today_str],
                    "type":     "dimension",
                }]
                log.info("sync.incremental_mode",
                         source=source_id,
                         from_date=last_str,
                         to_date=today_str)

        # 1. Fetch from source (blocks in thread for sync connectors)
        raw_records = await connector.fetch(extra_prompts=extra_prompts)
        if not raw_records:
            raise IngestionError(f"[{source_id}] Source returned no records.")

        # 2. Normalise: field_map + snake_case column names
        records = normalise_records(raw_records, cfg.field_map)

        # 3. Serialize nested objects (list/dict values) to JSON strings
        records = _serialize_nested(records)

        # 4. Bulk load into PostgreSQL
        df = pd.DataFrame(records)

        # Yıl guard: beklenen minimum yıl yeni veride yoksa eski veriyi koru.
        min_year = _MIN_YEAR_GUARD.get(cfg.target_table)
        if min_year and "yil" in df.columns:
            try:
                yillar: Set = set(df["yil"].dropna().astype(int).unique())
                if not any(y >= min_year for y in yillar):
                    log.warning(
                        "sync.year_guard_triggered",
                        source=source_id,
                        table=cfg.target_table,
                        new_years=sorted(yillar),
                        expected_min=min_year,
                    )
                    raise IngestionError(
                        f"[{source_id}] Yıl guard: yeni veri yalnızca {sorted(yillar)} "
                        f"yıllarını içeriyor, {min_year}+ bekleniyor. DB korundu."
                    )
            except IngestionError:
                raise
            except Exception as guard_exc:
                log.warning("sync.year_guard_error", error=str(guard_exc))

        if is_incremental and incr:
            rows_loaded = _incremental_load(df, cfg.target_table, incr.date_field)
        else:
            df.to_sql(
                cfg.target_table,
                _sync_engine,
                if_exists=if_exists,
                index=False,
                chunksize=5_000,
                method="multi",
            )
            rows_loaded = len(df)

        # 4. Refresh materialized views that depend on this table
        _refresh_views(cfg.target_table)

        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        finished_at = datetime.now(timezone.utc)

        result = SyncJobResult(
            job_id=job_id,
            source_id=source_id,
            status="completed",
            rows_loaded=rows_loaded,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )
        _write_job(job_id, source_id, "completed", started_at,
                   rows_loaded=rows_loaded, finished_at=finished_at,
                   duration_ms=duration_ms)
        log.info("sync.completed", source=source_id, rows=rows_loaded, ms=duration_ms)
        return result

    except Exception as exc:  # noqa: BLE001
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        finished_at = datetime.now(timezone.utc)
        _write_job(job_id, source_id, "failed", started_at,
                   error=str(exc), finished_at=finished_at, duration_ms=duration_ms)
        log.error("sync.failed", source=source_id, error=str(exc))
        return SyncJobResult(
            job_id=job_id,
            source_id=source_id,
            status="failed",
            error_message=str(exc),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )


def _get_last_date(table: str, date_col: str) -> Optional[date]:
    """Return the latest date in `date_col` of `table`, or None if table is empty/missing."""
    try:
        with _sync_engine.connect() as conn:
            row = conn.execute(text(
                f"SELECT MAX({date_col}::date) FROM {table}"  # noqa: S608
            )).fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        return None


def _incremental_load(df: "pd.DataFrame", table: str, date_col: str) -> int:
    """Delete rows whose date is in df, then insert df. Returns rows inserted."""
    if df.empty:
        return 0

    # Collect distinct dates in the new batch
    dates_in_batch = (
        df[date_col]
        .dropna()
        .apply(lambda v: str(v)[:10])   # "YYYY-MM-DD HH:MM:SS" → "YYYY-MM-DD"
        .unique()
        .tolist()
    )
    if not dates_in_batch:
        return 0

    with _sync_engine.begin() as conn:
        placeholders = ", ".join(f"'{d}'" for d in dates_in_batch)
        conn.execute(text(
            f"DELETE FROM {table} WHERE {date_col}::date IN ({placeholders})"  # noqa: S608
        ))

    df.to_sql(
        table,
        _sync_engine,
        if_exists="append",
        index=False,
        chunksize=5_000,
        method="multi",
    )
    log.info("sync.incremental_loaded",
             table=table, dates=dates_in_batch, rows=len(df))
    return len(df)


def _serialize_nested(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert list/dict values to JSON strings so psycopg2 can write them."""
    result = []
    for rec in records:
        row = {}
        for k, v in rec.items():
            row[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
        result.append(row)
    return result


def _refresh_views(table_name: str) -> None:
    views = _VIEW_DEPENDENCIES.get(table_name, [])
    if not views:
        return
    with _sync_engine.connect() as conn:
        for view in views:
            try:
                conn.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}"))
                conn.commit()
                log.info("sync.view_refreshed", view=view)
            except Exception as exc:  # noqa: BLE001
                log.warning("sync.view_refresh_failed", view=view, error=str(exc))


def _write_job(
    job_id: str,
    source_id: str,
    status: str,
    started_at: datetime,
    rows_loaded: Optional[int] = None,
    error: Optional[str] = None,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[float] = None,
) -> None:
    try:
        with _sync_engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO sync_jobs
                    (job_id, source_id, status, rows_loaded, error_message,
                     started_at, finished_at, duration_ms, created_at, updated_at)
                VALUES
                    (:job_id, :source_id, :status, :rows, :error,
                     :started, :finished, :duration, NOW(), NOW())
                ON CONFLICT (job_id) DO UPDATE SET
                    status       = EXCLUDED.status,
                    rows_loaded  = EXCLUDED.rows_loaded,
                    error_message = EXCLUDED.error_message,
                    finished_at  = EXCLUDED.finished_at,
                    duration_ms  = EXCLUDED.duration_ms,
                    updated_at   = NOW()
            """), {
                "job_id": job_id, "source_id": source_id, "status": status,
                "rows": rows_loaded, "error": error,
                "started": started_at, "finished": finished_at, "duration": duration_ms,
            })
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("sync.job_write_failed", error=str(exc))
