"""Pimland MCP — anlık (live) tekil ürün sorguları.

Bu modül, nightly sync yerine gerçek zamanlı tek ürün aramaları için
kullanılır. Call Center Agent gibi canlı stok/fiyat/detay ihtiyacı olan
servisler bu modülü kullanır.

Çağrılar `pimland_products.yaml`'daki connection + auth konfigürasyonunu
paylaşır; kendi token cache'ine sahiptir.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

_MCP_BASE = "https://agentup-mcp-test.pimland.com/30001"
_TOKEN_CACHE: Dict[str, Any] = {}
_TIMEOUT        = 30   # tekil ürün sorguları
_SEASON_TIMEOUT = 60   # toplu sezon sorguları (daha büyük payload)


# ── OAuth2 token ─────────────────────────────────────────────────────────────

async def _get_token(client: httpx.AsyncClient) -> Optional[str]:
    cached = _TOKEN_CACHE
    if cached.get("expires_at", 0) > time.time() + 30:
        return cached["access_token"]

    token_url = os.environ.get("PIMLAND_TOKEN_URL", "")
    if not token_url:
        log.warning("pimland_live.no_token_url")
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


# ── Tekil araç çağrısı ────────────────────────────────────────────────────────

async def _call_tool(
    client: httpx.AsyncClient,
    token: Optional[str],
    tool_name: str,
    args: Dict[str, Any],
) -> Any:
    url = f"{_MCP_BASE}/tools/{tool_name}/execute"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = await client.post(url, headers=headers, json=args, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # AgentUp envelope: content.data.result veya content.data
        result = (
            data.get("content", {}).get("data", {}).get("result")
            or data.get("content", {}).get("data")
            or data
        )
        return result
    except Exception as exc:
        log.warning("pimland_live.tool_error", tool=tool_name, error=str(exc))
        return None


# ── Yardımcı fonksiyonlar ────────────────────────────────────────────────────

def _filter_by_stock(result: Any, stock_code: str) -> List[Dict]:
    if not result:
        return []
    items = result if isinstance(result, list) else (result.get("items") or [])
    if not items:
        return []
    return [r for r in items if str(r.get("stockCode", "")) == str(stock_code)]


def _listify_result(raw: Any, stock_code: str) -> List[Dict]:
    """get_products_by_filter envelope'unu açar, stockCode'a göre filtreler."""
    if not raw:
        return []
    if isinstance(raw, list):
        return _filter_by_stock(raw, stock_code)
    if isinstance(raw, dict):
        result_obj = raw.get("result") or raw
        if isinstance(result_obj, dict):
            for key in ("products", "items", "data"):
                if isinstance(result_obj.get(key), list):
                    return _filter_by_stock(result_obj[key], stock_code)
        for key in ("products", "items", "data"):
            if isinstance(raw.get(key), list):
                return _filter_by_stock(raw[key], stock_code)
    return []


# ── Paralel ürün veri çekici ──────────────────────────────────────────────────

async def fetch_product_full(stock_code: str) -> Dict[str, Any]:
    """Bir ürün için tüm önemli verileri paralel çeker.

    Döner:
        {
            "details":      {...}  # kumaş, bakım, açıklama
            "stocks":       [...]  # beden × renk stok
            "sales_prices": [...]  # RPITL fiyatlar
            "erp_prices":   [...]  # alternatif ERP fiyatları
            "relations":    [...]  # ilişkili ürünler
        }
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            token = await _get_token(client)
        except Exception as exc:
            log.warning("pimland_live.token_failed", error=str(exc))
            token = None

        # Paralel çağrılar
        # get_products_with_squ kaldırıldı → get_products_by_filter kullan
        task_keys = ["details", "stocks", "sales_prices", "erp_prices",
                     "relations", "size_values", "sizes"]
        task_coros = [
            _call_tool(client, token,
                "post_api_Product_get_products_by_filter",
                {"stockCode": stock_code, "pageSize": 5, "pageNumber": 1}),
            _call_tool(client, token,
                "post_api_Product_get_product_stocks",
                {"stockCode": stock_code}),
            _call_tool(client, token,
                "post_api_Product_get_product_sales_prices",
                {"stockCode": stock_code}),
            _call_tool(client, token,
                "post_api_Product_get_product_erp_prices",
                {"stockCode": stock_code}),
            _call_tool(client, token,
                "post_api_Product_get_product_relations",
                {"stockCode": stock_code}),
            _call_tool(client, token,
                "post_api_Product_get_product_size_type_values",
                {"stockCode": stock_code}),
            _call_tool(client, token,
                "post_api_Product_get_product_sizes",
                {"stockCode": stock_code}),
        ]
        results = dict(zip(task_keys, await asyncio.gather(*task_coros)))

    # details: get_products_by_filter → result.products[] içinden stockCode ile bul
    det_raw = results.get("details")
    det_items: List[Dict] = []
    if isinstance(det_raw, dict):
        # get_products_by_filter: result.products
        result_obj = det_raw.get("result") or {}
        if isinstance(result_obj, dict) and isinstance(result_obj.get("products"), list):
            det_items = _filter_by_stock(result_obj["products"], stock_code)
        # fallback: diğer yolları dene
        if not det_items:
            for path in ("products", "items", "data"):
                if isinstance(det_raw.get(path), list):
                    det_items = _filter_by_stock(det_raw[path], stock_code)
                    if det_items:
                        break
    elif isinstance(det_raw, list):
        det_items = _filter_by_stock(det_raw, stock_code)

    def _listify(raw: Any) -> List[Dict]:
        if not raw:
            return []
        if isinstance(raw, list):
            return raw
        for k in ("items", "data", "result", "stocks", "prices"):
            if isinstance(raw.get(k), list):
                return raw[k]
        return []

    return {
        "details":      det_items[0] if det_items else None,
        "stocks":       _listify(results.get("stocks")),
        "sales_prices": _listify(results.get("sales_prices")),
        "erp_prices":   _listify(results.get("erp_prices")),
        "relations":    _listify(results.get("relations")),
        "size_values":  _listify(results.get("size_values")),
        "sizes":        _listify(results.get("sizes")),
    }


async def fetch_season_products(season_code: str) -> Dict[str, Dict]:
    """
    Bir sezonun tüm ürünlerini MCP'den çeker.
    get_products_by_filter ile season filtresi kullanır.
    Döner: {stockCode: product_dict}
    """
    result_map: Dict[str, Dict] = {}
    page = 1

    async with httpx.AsyncClient(timeout=_SEASON_TIMEOUT) as client:
        try:
            token = await _get_token(client)
        except Exception as exc:
            log.warning("pimland_live.token_failed", error=str(exc))
            return result_map

        while True:
            # pageNumber: None → sunucu sıfırlama hatası vermez (1 gönderince 0 sonuç döner)
            params: Dict[str, Any] = {"season": season_code, "pageSize": 25}
            if page > 1:
                params["pageNumber"] = page
            raw = await _call_tool(client, token,
                "post_api_Product_get_products_by_filter", params
            )
            if not raw:
                break

            # Response: result.products veya result direkt liste
            if isinstance(raw, dict):
                result_obj = raw.get("result") or raw
                if isinstance(result_obj, dict):
                    products = result_obj.get("products") or []
                    total_pages = result_obj.get("totalPageCount", 1)
                else:
                    products = []
                    total_pages = 1
            elif isinstance(raw, list):
                products = raw
                total_pages = 1
            else:
                break

            for p in products:
                if isinstance(p, dict) and p.get("stockCode"):
                    result_map[p["stockCode"]] = p

            log.debug("pimland_live.season_page",
                      season=season_code, page=page,
                      fetched=len(products), total=len(result_map))

            if page >= total_pages:
                break
            page += 1

    log.info("pimland_live.season_done", season=season_code, total=len(result_map))
    return result_map


async def search_products(
    brand_id: Optional[str] = None,
    season_code: Optional[str] = None,
    group_id: Optional[str] = None,
    page_size: int = 10,
) -> List[Dict]:
    """Filtreyle ürün ara (keşif sorguları için)."""
    args: Dict[str, Any] = {"pageSize": page_size, "pageNumber": 1}
    if brand_id:   args["brandId"]   = brand_id
    if season_code: args["seasonCode"] = season_code
    if group_id:   args["productGroupId"] = group_id

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _get_token(client)
        result = await _call_tool(client, token,
            "post_api_Product_get_products_by_filter", args)
        if not result:
            return []
        for k in ("items", "data", "products", "result"):
            if isinstance(result.get(k) if isinstance(result, dict) else None, list):
                return result[k]
        return result if isinstance(result, list) else []
