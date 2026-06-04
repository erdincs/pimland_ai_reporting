"""Toplu MCP ürün sorgulama.

asyncio.gather + Semaphore ile max 10 eşzamanlı istek.
Büyük listeleri (100+ ürün) verimli çeker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from app.connectors.pimland_live import fetch_product_full
from app.core.logging import get_logger

log = get_logger(__name__)

_SEMAPHORE_LIMIT = 10   # aynı anda max 10 MCP isteği
_MAX_BATCH       = 100  # tek seferde max 100 ürün


async def _fetch_one(sku: str, sem: asyncio.Semaphore) -> Dict[str, Any]:
    async with sem:
        try:
            data = await fetch_product_full(sku)
            return {"sku": sku, "ok": True, "data": data}
        except Exception as e:
            log.warning("batch_mcp.fetch_failed", sku=sku, error=str(e))
            return {"sku": sku, "ok": False, "data": {}}


async def fetch_batch(
    skus: List[str],
    limit: int = _MAX_BATCH,
) -> Dict[str, Dict[str, Any]]:
    """
    Birden fazla SKU'yu paralel çeker.
    Döner: {sku: {details, stocks, sales_prices, ...}}
    """
    unique = list(dict.fromkeys(s for s in skus if s))[:limit]
    if not unique:
        return {}

    t0  = time.perf_counter()
    sem = asyncio.Semaphore(_SEMAPHORE_LIMIT)

    results = await asyncio.gather(*[_fetch_one(sku, sem) for sku in unique])

    sure = round(time.perf_counter() - t0, 1)
    ok   = sum(1 for r in results if r["ok"])
    log.info("batch_mcp.done", total=len(unique), ok=ok, sure_sn=sure)

    return {r["sku"]: r["data"] for r in results if r["ok"]}


def extract_product_summary(data: Dict[str, Any], sku: str) -> Dict[str, Any]:
    """MCP yanıtından sade özet çıkar (agent context için)."""
    details = data.get("details") or {}
    stocks  = data.get("stocks") or []
    prices  = data.get("sales_prices") or []
    sizes   = data.get("size_values") or []

    # Fiyat (RPITL öncelikli)
    fiyat = None
    for p in prices:
        if isinstance(p, dict) and "RPITL" in str(p.get("priceTypeCode", "")):
            fiyat = p.get("price") or p.get("calculatedPrice")
            break
    if fiyat is None and prices:
        first = prices[0] if isinstance(prices[0], dict) else {}
        fiyat = first.get("price") or first.get("calculatedPrice")

    # Stok özeti
    stok_var = any(
        (s.get("stock") or s.get("quantity") or 0) > 0
        for s in stocks if isinstance(s, dict)
    )

    # Kumaş
    kumas = (
        details.get("fabricMaterialName")
        or details.get("mainMaterialContent", "")
        or ""
    )
    # İlk barcode'dan
    if not kumas:
        barcodes = details.get("barcodes") or []
        if barcodes and isinstance(barcodes[0], dict):
            kumas = barcodes[0].get("mainMaterialContent", "")

    # Bedenler
    bedenler = sorted({
        s.get("sizeCode") or s.get("productSizeCode") or s.get("sizeName", "")
        for s in sizes if isinstance(s, dict)
        if s.get("sizeCode") or s.get("productSizeCode")
    })

    return {
        "sku":       sku,
        "ad":        details.get("description") or details.get("productName") or "",
        "fiyat":     fiyat,
        "stok_var":  stok_var,
        "kumas":     kumas[:200] if kumas else "",
        "bedenler":  bedenler[:10],
    }
