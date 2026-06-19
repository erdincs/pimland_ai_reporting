"""Pimland kategori önbellek — urun_kodu → URL slug.

Akış:
  1. get_ecom_category_tree(catalog=0001) → L1 kategoriler
  2. Her L1 kategori için get_ecom_category_products paralel çağrı
  3. stockCode → slug haritası Redis'e yazılır (TTL 24 saat)
  4. Redis yoksa modül düzeyi in-memory dict (process ömrü)

Kullanım:
  slug = await category_slug_for("12345678900")  # → "elbise"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import httpx

from app.utils.adl_url import slugify

log = logging.getLogger(__name__)

_MCP_BASE = "https://agentup-mcp-test.pimland.com/30001"
_CATALOG   = "0001"
_CACHE_KEY = "brief:cat_map"
_TTL       = 86400  # 24 saat
_TIMEOUT   = 15

_mem_cache: dict[str, str] = {}   # stockCode → slug, in-process fallback


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _token(client: httpx.AsyncClient) -> Optional[str]:
    try:
        r = await client.post(
            os.environ.get("PIMLAND_TOKEN_URL", "https://ids.pimland.com/connect/token"),
            data={
                "grant_type":    "password",
                "client_id":     os.environ.get("PIMLAND_CLIENT_ID", ""),
                "client_secret": os.environ.get("PIMLAND_CLIENT_SECRET", ""),
                "username":      os.environ.get("PIMLAND_USERNAME", ""),
                "password":      os.environ.get("PIMLAND_PASSWORD", ""),
                "scope":         os.environ.get("PIMLAND_SCOPE", ""),
            },
            timeout=10,
        )
        return r.json().get("access_token")
    except Exception as exc:
        log.warning("pimland_category.token_failed", extra={"err": str(exc)})
        return None


async def _call(client: httpx.AsyncClient, token: Optional[str], tool: str, payload: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = await client.post(
            f"{_MCP_BASE}/tools/{tool}/execute",
            headers=headers, json=payload, timeout=_TIMEOUT,
        )
        content = r.json().get("content", {})
        if content.get("success") and content.get("data", {}).get("isSuccessful"):
            return content["data"]["result"] or {}
    except Exception as exc:
        log.warning("pimland_category.call_failed", extra={"tool": tool, "err": str(exc)})
    return {}


# ── Tree + products ───────────────────────────────────────────────────────────

def _flatten_l1(tree_result: dict) -> list[dict]:
    """Root (Kategoriler) → L1 kategoriler."""
    categories = tree_result.get("categories", [])
    if not categories:
        return []
    root_children = categories[0].get("children", []) if categories else []
    return root_children


async def _fetch_map(client: httpx.AsyncClient, token: Optional[str]) -> dict[str, str]:
    """stockCode → slug haritası döndürür."""
    tree = await _call(client, token, "post_api_Product_get_ecom_category_tree", {"catalog": _CATALOG})
    l1_cats = _flatten_l1(tree)
    if not l1_cats:
        log.warning("pimland_category.empty_tree")
        return {}

    async def _products_for(cat: dict) -> list[tuple[str, str]]:
        slug = slugify(cat["name"])
        ref  = cat["referenceCode"]
        result = await _call(
            client, token,
            "post_api_Product_get_ecom_category_products",
            {"catalog": _CATALOG, "category": ref},
        )
        return [(p["stockCode"], slug) for p in result.get("products", []) if p.get("stockCode")]

    results = await asyncio.gather(*[_products_for(c) for c in l1_cats], return_exceptions=True)

    mapping: dict[str, str] = {}
    for r in results:
        if isinstance(r, list):
            for stock_code, slug in r:
                mapping.setdefault(stock_code, slug)   # ilk bulunan L1 kategori kazanır
    return mapping


# ── Public API ────────────────────────────────────────────────────────────────

async def warm_category_cache() -> int:
    """Kategori haritasını Redis'e (veya in-memory) yükler. Yüklenen ürün sayısını döndürür."""
    async with httpx.AsyncClient() as client:
        token   = await _token(client)
        mapping = await _fetch_map(client, token)

    if not mapping:
        return 0

    global _mem_cache
    _mem_cache = mapping

    try:
        from app.services.cache_service import get_redis
        redis = await get_redis()
        await redis.set(_CACHE_KEY, json.dumps(mapping), ex=_TTL)
        log.info("pimland_category.warmed_redis", extra={"count": len(mapping)})
    except Exception:
        log.info("pimland_category.warmed_memory", extra={"count": len(mapping)})

    return len(mapping)


async def category_slug_for(urun_kodu: str) -> str:
    """urun_kodu → URL slug. Bulunamazsa 'urun' döner."""
    if urun_kodu in _mem_cache:
        return _mem_cache[urun_kodu]

    try:
        from app.services.cache_service import get_redis
        redis = await get_redis()
        raw = await redis.get(_CACHE_KEY)
        if raw:
            mapping = json.loads(raw)
            _mem_cache.update(mapping)
            return mapping.get(urun_kodu, "urun")
    except Exception:
        pass

    return "urun"


async def get_category_map() -> dict[str, str]:
    """Tüm haritayı döndürür — brief gen başında bir kez çağrılır."""
    try:
        from app.services.cache_service import get_redis
        redis = await get_redis()
        raw = await redis.get(_CACHE_KEY)
        if raw:
            mapping = json.loads(raw)
            _mem_cache.update(mapping)
            return mapping
    except Exception:
        pass

    if _mem_cache:
        return _mem_cache

    count = await warm_category_cache()
    log.info("pimland_category.lazy_warm", extra={"count": count})
    return _mem_cache
