"""Redis tabanlı oturum dosya yönetimi.

Dosyalar 1 saat TTL ile saklanır.
Excel geçici tabloları oturum sonunda temizlenir.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

SESSION_TTL = 3600  # 1 saat

_redis: aioredis.Redis = aioredis.from_url(
    settings.redis_url, encoding="utf-8", decode_responses=True
)


def _file_key(session_id: str, file_id: str) -> str:
    return f"session:{session_id}:file:{file_id}"


def _session_index_key(session_id: str) -> str:
    return f"session:{session_id}:files"


async def store_file(session_id: str, file_meta: Dict[str, Any]) -> str:
    """Dosya meta'sını Redis'e yaz, file_id döndür."""
    file_id = str(uuid.uuid4())[:8]
    key     = _file_key(session_id, file_id)

    await _redis.setex(key, SESSION_TTL, json.dumps(file_meta, default=str, ensure_ascii=False))

    # Oturum index'ine ekle
    idx_key = _session_index_key(session_id)
    await _redis.sadd(idx_key, file_id)
    await _redis.expire(idx_key, SESSION_TTL)

    log.info("session_store.saved", session_id=session_id, file_id=file_id, type=file_meta.get("type"))
    return file_id


async def get_file(session_id: str, file_id: str) -> Optional[Dict[str, Any]]:
    raw = await _redis.get(_file_key(session_id, file_id))
    if not raw:
        return None
    return json.loads(raw)


async def get_session_files(session_id: str) -> List[Dict[str, Any]]:
    idx_key = _session_index_key(session_id)
    file_ids = await _redis.smembers(idx_key)

    files = []
    for fid in file_ids:
        f = await get_file(session_id, fid)
        if f:
            f["file_id"] = fid
            files.append(f)
    return files


async def delete_file(
    session_id: str,
    file_id: str,
    db_session=None,
) -> bool:
    """Dosyayı sil, Excel ise PostgreSQL tablosunu da drop et."""
    f = await get_file(session_id, file_id)
    if not f:
        return False

    # Excel geçici tabloları temizle
    if f.get("type") == "dataframe" and db_session:
        from sqlalchemy import text
        for t in f.get("tables", []):
            pg_table = t.get("pg_table", "")
            if pg_table.startswith("tmp_"):
                try:
                    await db_session.execute(text(f"DROP TABLE IF EXISTS {pg_table}"))
                    await db_session.commit()
                    log.info("session_store.table_dropped", pg_table=pg_table)
                except Exception as e:
                    log.warning("session_store.drop_failed", pg_table=pg_table, err=str(e))

    await _redis.delete(_file_key(session_id, file_id))
    await _redis.srem(_session_index_key(session_id), file_id)
    return True


async def cleanup_session(session_id: str, db_session=None) -> int:
    """Oturumdaki tüm dosyaları ve geçici tabloları temizle."""
    idx_key  = _session_index_key(session_id)
    file_ids = list(await _redis.smembers(idx_key))

    for fid in file_ids:
        await delete_file(session_id, fid, db_session)

    await _redis.delete(idx_key)
    log.info("session_store.cleanup", session_id=session_id, count=len(file_ids))
    return len(file_ids)
