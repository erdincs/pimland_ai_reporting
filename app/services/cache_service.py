"""Redis-backed cache for repeated queries (and, later, session state).

Caches the full query response keyed by a hash of (question + filters), so
identical analyses skip both the LLM call and the DB round-trip. TTL-bounded.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_redis: aioredis.Redis = aioredis.from_url(
    settings.redis_url, encoding="utf-8", decode_responses=True
)

_PREFIX = "query"


def make_key(question: str, filters: Optional[dict]) -> str:
    payload = json.dumps(
        {"q": question.strip().lower(), "f": filters or {}},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{_PREFIX}:{digest}"


async def get(key: str) -> Optional[Dict[str, Any]]:
    raw = await _redis.get(key)
    if raw is None:
        return None
    log.info("cache.hit", key=key)
    return json.loads(raw)


async def set(key: str, value: Dict[str, Any], ttl: Optional[int] = None) -> None:
    await _redis.set(
        key,
        json.dumps(value, default=str, ensure_ascii=False),
        ex=ttl or settings.cache_ttl_seconds,
    )


async def ping() -> bool:
    try:
        return bool(await _redis.ping())
    except Exception as exc:  # noqa: BLE001
        log.warning("cache.unavailable", error=str(exc))
        return False
