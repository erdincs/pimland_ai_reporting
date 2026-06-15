"""Gerçek zamanlı ürün zenginleştirme servisi.

Pimland MCP'den per-SKU verileri paralel çeker:
  - Stok durumu (renk/beden bazında)
  - Satış fiyatları
  - ERP maliyet fiyatı + KDV
  - Beden bilgileri

Redis'te TTL'li cache:
  - Stok: 15 dakika (sık değişir)
  - Fiyat/beden: 2 saat
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services import cache_service

log = get_logger(__name__)

_BASE_URL = "https://agentup-mcp-test.pimland.com/30001"
_TOKEN_URL = "https://ids.pimland.com/connect/token"
_TOKEN_CACHE: Dict[str, Any] = {}


async def _get_token(client: httpx.AsyncClient) -> str:
    """OAuth2 ROPC token — 30 saniyelik buffer ile cache."""
    if _TOKEN_CACHE.get("expires_at", 0) > time.time() + 30:
        return _TOKEN_CACHE["access_token"]

    resp = await client.post(_TOKEN_URL, data={
        "grant_type":    "password",
        "client_id":     settings.pimland_client_id or "",
        "client_secret": settings.pimland_client_secret or "",
        "username":      settings.pimland_username or "",
        "password":      settings.pimland_password or "",
        "scope":         settings.pimland_scope or "",
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
    token: str,
    tool: str,
    sku: str,
    extra_params: Optional[Dict] = None,
) -> Any:
    body = {"stockCode": sku, "includedRevision": False}
    if extra_params:
        body.update(extra_params)
    try:
        resp = await client.post(
            f"{_BASE_URL}/tools/post_api_Product_{tool}/execute",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("content", {}).get("data", {}).get("result", {})
    except Exception as exc:
        log.warning("product_live.tool_failed", tool=tool, sku=sku, error=str(exc))
        return None


def _parse_stocks(raw: Optional[Dict]) -> List[Dict]:
    if not raw or not raw.get("productStocks"):
        return []
    stocks = []
    for s in raw["productStocks"]:
        stocks.append({
            "beden": s.get("sizeName"),
            "renk_kodu": s.get("skuCode", "")[-3:] if s.get("skuCode") else None,
            "stok": int(s.get("stock", 0)),
            "ean": s.get("eanCode"),
            "guncelleme": s.get("lastUpdatedDate", "")[:10],
        })
    return sorted(stocks, key=lambda x: x.get("beden", "") or "")


def _parse_prices(raw: Optional[Dict]) -> List[Dict]:
    if not raw or not raw.get("salesPrices"):
        return []
    return [
        {
            "grup": p.get("priceGroupName"),
            "fiyat": p.get("calculatedPrice") or p.get("price"),
            "para_birimi": p.get("currencyName") or "TRY",
        }
        for p in raw["salesPrices"]
        if p.get("calculatedPrice") or p.get("price")
    ][:10]


def _parse_erp_price(raw: Optional[Dict]) -> Optional[Dict]:
    if not raw or not raw.get("stockCode"):
        return None
    return {
        "satis_fiyati": raw.get("retailPrice"),
        "maliyet": raw.get("price"),
        "onceki_fiyat": raw.get("previousPrice"),
        "kdv": raw.get("taxRate"),
        "para_birimi": raw.get("currencyName", "TRY"),
        "indirimli": raw.get("isDiscounted", False),
        "fiyat_tarihi": (raw.get("priceDate") or "")[:10],
    }


def _parse_sizes(raw: Optional[Dict]) -> List[str]:
    if not raw or not raw.get("productSizes"):
        return []
    return [s.get("sizeName", "") for s in raw["productSizes"] if s.get("sizeName")]


async def get_product_live(sku: str) -> Dict[str, Any]:
    """SKU için canlı stok/fiyat/beden verilerini paralel çek, Redis'te cache'le."""
    cache_key = f"product_live:{sku}"

    cached = await cache_service.get(cache_key)
    if cached:
        cached["_cached"] = True
        return cached

    async with httpx.AsyncClient(timeout=20) as client:
        token = await _get_token(client)

        stocks_raw, prices_raw, erp_raw, sizes_raw = await asyncio.gather(
            _call(client, token, "get_product_stocks",       sku),
            _call(client, token, "get_product_sales_prices", sku),
            _call(client, token, "get_product_erp_prices",   sku),
            _call(client, token, "get_product_sizes",        sku),
        )

    result = {
        "sku":         sku,
        "stok":        _parse_stocks(stocks_raw),
        "satis_fiyat": _parse_prices(prices_raw),
        "erp_fiyat":   _parse_erp_price(erp_raw),
        "bedenler":    _parse_sizes(sizes_raw),
        "toplam_stok": sum(s["stok"] for s in _parse_stocks(stocks_raw)),
        "_cached":     False,
    }

    await cache_service.set(cache_key, {k: v for k, v in result.items() if k != "_cached"},
                            ttl=900)

    log.info("product_live.fetched", sku=sku,
             toplam_stok=result["toplam_stok"],
             fiyat_sayisi=len(result["satis_fiyat"]))
    return result
