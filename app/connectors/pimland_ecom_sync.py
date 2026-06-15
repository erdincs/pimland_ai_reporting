"""Pimland e-ticaret katalog/kategori/sıralama senkronizasyonu.

Standart McpConnector ile çözülemeyen çok adımlı sync:
  1. pim_ecom_catalogs tablosundan katalog kodlarını oku
  2. Her katalog için get_ecom_category_tree → pim_ecom_category_tree
  3. Her katalog + kategori için:
     - get_ecom_category_products  → pim_ecom_category_products
     - get_ecom_page_planner_products → pim_ecom_page_planner

Tablolar tamamen yeniden yüklenir (TRUNCATE + INSERT).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_BASE_URL = "https://agentup-mcp-test.pimland.com/30001"
_TIMEOUT = 30
_TOKEN_CACHE: Dict[str, Any] = {}
_sync_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _get_token(client: httpx.AsyncClient) -> Optional[str]:
    if _TOKEN_CACHE.get("expires_at", 0) > time.time() + 30:
        return _TOKEN_CACHE["access_token"]

    token_url = os.environ.get("PIMLAND_TOKEN_URL", "")
    if not token_url:
        return None

    resp = await client.post(token_url, data={
        "grant_type":    "password",
        "client_id":     os.environ.get("PIMLAND_CLIENT_ID", ""),
        "client_secret": os.environ.get("PIMLAND_CLIENT_SECRET", ""),
        "username":      os.environ.get("PIMLAND_USERNAME", ""),
        "password":      os.environ.get("PIMLAND_PASSWORD", ""),
        "scope":         os.environ.get("PIMLAND_SCOPE", ""),
    })
    resp.raise_for_status()
    data = resp.json()
    _TOKEN_CACHE.update({
        "access_token": data["access_token"],
        "expires_at":   time.time() + data.get("expires_in", 3600),
    })
    return data["access_token"]


async def _call(
    client: httpx.AsyncClient,
    token: Optional[str],
    tool_name: str,
    args: Dict[str, Any],
) -> Any:
    url = f"{_BASE_URL}/tools/{tool_name}/execute"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = await client.post(url, headers=headers, json=args, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("content", {}).get("data", {}).get("result")
            or data.get("content", {}).get("data")
            or data
        )
    except Exception as exc:
        log.warning("ecom_sync.tool_error", tool=tool_name, error=str(exc))
        return None


# ── Katalog listesi ───────────────────────────────────────────────────────────

def _load_catalogs() -> List[str]:
    """pim_ecom_catalogs tablosundan aktif katalog kodlarını oku."""
    try:
        with _sync_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT catalog_code FROM pim_ecom_catalogs WHERE catalog_code IS NOT NULL")
            ).fetchall()
            return [r[0] for r in rows if r[0]]
    except Exception as exc:
        log.warning("ecom_sync.catalog_load_failed", error=str(exc))
        return []


# ── Kategori ağacı ────────────────────────────────────────────────────────────

def _flatten_tree(
    nodes: Any,
    catalog_code: str,
    parent_id: Optional[str] = None,   # üst kategorinin category_id'si (integer ID)
    level: int = 0,
) -> List[Dict[str, Any]]:
    """Nested kategori ağacını düz satır listesine dönüştür.

    Alan eşlemeleri (Pimland API):
      categoryId    → category_id  (integer, parent takibi için)
      referenceCode → category_code (API çağrılarında kullanılan kod)
    """
    rows = []
    if not nodes:
        return rows
    if isinstance(nodes, dict):
        nodes = [nodes]
    for node in (nodes or []):
        if not isinstance(node, dict):
            continue
        cat_id   = str(node.get("categoryId") or node.get("id") or "")
        # referenceCode API'ye gönderilen gerçek kategori kodudur
        cat_code = str(
            node.get("referenceCode") or node.get("code")
            or node.get("categoryCode") or cat_id
        )
        cat_name = node.get("name") or node.get("categoryName")
        rows.append({
            "catalog_code":  catalog_code,
            "category_code": cat_code,
            "category_id":   cat_id,
            "category_name": cat_name,
            "parent_id":     parent_id,
            "level":         level,
            "sort_order":    node.get("order") or node.get("sortOrder"),
            "is_active":     node.get("isActive", True),
            "sync_updated_at": datetime.now(timezone.utc),
        })
        children = node.get("children") or node.get("subCategories") or []
        rows.extend(_flatten_tree(children, catalog_code, parent_id=cat_id, level=level + 1))
    return rows


async def _sync_category_tree(
    client: httpx.AsyncClient,
    token: Optional[str],
    catalog_code: str,
) -> List[Dict[str, Any]]:
    raw = await _call(client, token, "post_api_Product_get_ecom_category_tree",
                      {"catalog": catalog_code})
    if not raw:
        return []

    # Yanıt: doğrudan liste veya {categories: [...]} veya {result: [...]}
    if isinstance(raw, list):
        nodes = raw
    elif isinstance(raw, dict):
        nodes = (raw.get("categories") or raw.get("result")
                 or raw.get("items") or [raw])
    else:
        nodes = []

    rows = _flatten_tree(nodes, catalog_code)
    # Aynı (catalog_code, category_code) çifti birden fazla kez gelebilir — son olanı tut
    seen: dict = {}
    for r in rows:
        seen[(r["catalog_code"], r["category_code"])] = r
    rows = list(seen.values())
    log.info("ecom_sync.tree_fetched", catalog=catalog_code, categories=len(rows))
    return rows


# ── Kategori ürünleri ve sıralama ────────────────────────────────────────────

def _parse_category_products(
    raw: Any,
    catalog_code: str,
    category_code: str,
) -> List[Dict[str, Any]]:
    """Her ürün için colors listesini açarak (stockCode, renk_kodu) çiftleri üretir."""
    if not raw:
        return []
    items = (
        raw if isinstance(raw, list)
        else (raw.get("products") or raw.get("items") or raw.get("result") or [])
    )
    now = datetime.now(timezone.utc)
    rows = []
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        stock_code = item.get("stockCode") or item.get("productCode")
        if not stock_code:
            continue
        colors = item.get("colors") or []
        if colors:
            for color in colors:
                renk = str(color.get("referenceCode") or color.get("colorCode") or "")
                rows.append({
                    "catalog_code":  catalog_code,
                    "category_code": category_code,
                    "urun_kodu":     str(stock_code),
                    "renk_kodu":     renk,
                    "is_active":     item.get("isActive", True),
                    "sync_updated_at": now,
                })
        else:
            # Renk bilgisi yoksa boş string ile kaydet
            rows.append({
                "catalog_code":  catalog_code,
                "category_code": category_code,
                "urun_kodu":     str(stock_code),
                "renk_kodu":     "",
                "is_active":     item.get("isActive", True),
                "sync_updated_at": now,
            })
    return rows


def _parse_page_planner(
    raw: Any,
    catalog_code: str,
    category_code: str,
) -> List[Dict[str, Any]]:
    """Sayfa sıralama verisi — her (stockCode, colorReferenceCode) çifti ayrı satır."""
    if not raw:
        return []
    items = (
        raw if isinstance(raw, list)
        else (raw.get("products") or raw.get("items") or raw.get("result") or [])
    )
    now = datetime.now(timezone.utc)
    rows = []
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        stock_code = item.get("stockCode") or item.get("productCode")
        color_code = (
            item.get("colorReferenceCode") or item.get("colorCode")
            or item.get("color") or ""
        )
        if not stock_code:
            continue
        rows.append({
            "catalog_code":  catalog_code,
            "category_code": category_code,
            "urun_kodu":     str(stock_code),
            "renk_kodu":     str(color_code),
            "sira_no":       item.get("order") or item.get("sortOrder") or item.get("rank"),
            "display_type":  item.get("displayType") or item.get("type"),
            "sync_updated_at": now,
        })
    return rows


async def _sync_category(
    client: httpx.AsyncClient,
    token: Optional[str],
    catalog_code: str,
    category_code: str,
) -> Tuple[List[Dict], List[Dict]]:
    """Tek katalog+kategori için ürün ve sıralama verilerini paralel çek."""
    cat_products_raw, planner_raw = await asyncio.gather(
        _call(client, token, "post_api_Product_get_ecom_category_products",
              {"catalog": catalog_code, "category": category_code}),
        _call(client, token, "post_api_Product_get_ecom_page_planner_products",
              {"catalog": catalog_code, "category": category_code}),
    )
    products = _parse_category_products(cat_products_raw, catalog_code, category_code)
    planner  = _parse_page_planner(planner_raw, catalog_code, category_code)
    return products, planner


# ── DB yazma ─────────────────────────────────────────────────────────────────

def _bulk_upsert(table: str, rows: List[Dict[str, Any]], pk_cols: List[str]) -> int:
    """TRUNCATE + INSERT — view bağımlılıklarını bozmaz."""
    if not rows:
        return 0
    import pandas as pd
    import json

    clean = []
    for r in rows:
        row = {}
        for k, v in r.items():
            if isinstance(v, datetime):
                row[k] = v.isoformat()
            elif isinstance(v, (list, dict)):
                row[k] = json.dumps(v, ensure_ascii=False)
            else:
                row[k] = v
        clean.append(row)

    df = pd.DataFrame(clean)
    with _sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table}"))
    df.to_sql(table, _sync_engine, if_exists="append", index=False,
              chunksize=5_000, method="multi")
    return len(df)


def _refresh_views() -> None:
    views = [
        "mv_ecom_product_placement",
    ]
    with _sync_engine.connect() as conn:
        for view in views:
            try:
                conn.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}"))
                conn.commit()
                log.info("ecom_sync.view_refreshed", view=view)
            except Exception as exc:
                log.warning("ecom_sync.view_refresh_failed", view=view, error=str(exc))


# ── Ana sync fonksiyonu ───────────────────────────────────────────────────────

async def run_ecom_sync() -> Dict[str, Any]:
    """Tüm kataloglar için kategori ağacı + ürün + sıralama verisini senkronize et."""
    t0 = time.perf_counter()
    catalog_codes = _load_catalogs()
    if not catalog_codes:
        log.warning("ecom_sync.no_catalogs",
                    hint="pimland_ecom_catalogs sync'i önce çalıştır")
        return {"status": "skipped", "reason": "no_catalogs"}

    log.info("ecom_sync.started", catalogs=catalog_codes)

    all_tree_rows:     List[Dict] = []
    all_product_rows:  List[Dict] = []
    all_planner_rows:  List[Dict] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _get_token(client)

        for catalog_code in catalog_codes:
            # 1. Kategori ağacı
            tree_rows = await _sync_category_tree(client, token, catalog_code)
            all_tree_rows.extend(tree_rows)

            # 2. Her kategori için ürün + sıralama (max 10 paralel)
            category_codes = list({r["category_code"] for r in tree_rows})
            sem = asyncio.Semaphore(10)

            async def _fetch_cat(cat_code: str) -> Tuple[List, List]:
                async with sem:
                    return await _sync_category(client, token, catalog_code, cat_code)

            results = await asyncio.gather(
                *[_fetch_cat(c) for c in category_codes],
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, Exception):
                    log.warning("ecom_sync.category_error", error=str(res))
                    continue
                prods, planr = res
                all_product_rows.extend(prods)
                all_planner_rows.extend(planr)

    # DB'ye yaz
    tree_count    = _bulk_upsert("pim_ecom_category_tree",    all_tree_rows,
                                  ["catalog_code", "category_code"])
    product_count = _bulk_upsert("pim_ecom_category_products", all_product_rows,
                                  ["catalog_code", "category_code", "urun_kodu", "renk_kodu"])
    planner_count = _bulk_upsert("pim_ecom_page_planner",      all_planner_rows,
                                  ["catalog_code", "category_code", "urun_kodu", "renk_kodu"])

    _refresh_views()

    duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("ecom_sync.completed",
             catalogs=len(catalog_codes),
             tree=tree_count, products=product_count, planner=planner_count,
             ms=duration_ms)
    return {
        "status":    "completed",
        "catalogs":  len(catalog_codes),
        "tree":      tree_count,
        "products":  product_count,
        "planner":   planner_count,
        "duration_ms": duration_ms,
    }
