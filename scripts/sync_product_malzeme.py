#!/usr/bin/env python
"""Ürün malzeme detaylarını Pimland'dan çekip pim_product_malzeme tablosuna yükler.

rawMaterials (ana malzemeler) ve auxiliaryMaterials (yardımcı malzemeler) birlikte
TRUNCATE + append pattern ile yüklenir, mv_product_malzeme_ozet view'ı yenilenir.

    python scripts/sync_product_malzeme.py
    python scripts/sync_product_malzeme.py --limit 50   # test için
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd
from sqlalchemy import create_engine, text

# dotenv load — settings'ten önce
import os
from pathlib import Path

_env_path = Path(__file__).resolve().parents[1] / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger("sync_product_malzeme")

_BASE_URL = "https://agentup-mcp-test.pimland.com/30001"
_TOKEN_URL = "https://ids.pimland.com/connect/token"
_TOKEN_CACHE: Dict[str, Any] = {}

_TABLE = "pim_product_malzeme"
_VIEW  = "mv_product_malzeme_ozet"

_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)


def _get_token_sync(client: httpx.Client) -> str:
    if _TOKEN_CACHE.get("expires_at", 0) > time.time() + 30:
        return _TOKEN_CACHE["access_token"]
    resp = client.post(_TOKEN_URL, data={
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


def _fetch_malzeme(client: httpx.Client, token: str, urun_kodu: str) -> Optional[Dict]:
    try:
        resp = client.post(
            f"{_BASE_URL}/tools/post_api_Product_get_product_financial_datas/execute",
            headers={"Authorization": f"Bearer {token}"},
            json={"stockCode": urun_kodu, "includedRevision": False},
            timeout=20,
        )
        resp.raise_for_status()
        result = resp.json().get("content", {}).get("data", {}).get("result", {})
        financials = result.get("financialDatas", [])
        return financials[0] if financials else None
    except Exception as exc:
        log.warning("malzeme.fetch_failed", urun_kodu=urun_kodu, error=str(exc))
        return None


def _parse_raw_materials(urun_kodu: str, items: List[Dict]) -> List[Dict]:
    rows = []
    for item in items:
        rows.append({
            "urun_kodu":         urun_kodu,
            "malzeme_tipi":      "ana",
            "malzeme_grup_kodu": str(item.get("materialGroupCode", "") or ""),
            "malzeme_grup_adi":  item.get("materialGroupName"),
            "stok_kodu":         item.get("materialStockCode") or None,
            "dis_malzeme":       item.get("externalMaterial") or None,
            "miktar":            item.get("amount"),
            "birim_fiyat":       item.get("unitPrice"),
            "birim_adi":         item.get("unitName"),
            "birim_kodu":        item.get("unitCode"),
            "fire_orani":        item.get("wastageAmount"),
            "doviz":             item.get("currency"),
            "kur":               item.get("exchangeRate"),
            "toplam_tl":         item.get("total"),
            "doviz_degeri":      item.get("currencyValue"),
            "renk_adi":          None,
            "renk_kodu":         None,
        })
    return rows


def _parse_aux_materials(urun_kodu: str, items: List[Dict]) -> List[Dict]:
    rows = []
    for item in items:
        rows.append({
            "urun_kodu":         urun_kodu,
            "malzeme_tipi":      "yardimci",
            "malzeme_grup_kodu": str(item.get("materialGroupCode", "") or ""),
            "malzeme_grup_adi":  item.get("materialGroupName"),
            "stok_kodu":         item.get("materialStockCode") or None,
            "dis_malzeme":       item.get("externalMaterial") or None,
            "miktar":            item.get("amount"),
            "birim_fiyat":       item.get("unitPrice"),
            "birim_adi":         item.get("unitName"),
            "birim_kodu":        item.get("unitCode"),
            "fire_orani":        item.get("wastageAmount"),
            "doviz":             item.get("currency"),
            "kur":               item.get("exchangeRate"),
            "toplam_tl":         item.get("total"),
            "doviz_degeri":      item.get("currencyValue"),
            "renk_adi":          item.get("colorName") or None,
            "renk_kodu":         item.get("colorCode") or None,
        })
    return rows


def _load_urun_kodlari(limit: Optional[int] = None) -> List[str]:
    query = "SELECT urun_kodu FROM pim_products ORDER BY urun_kodu"
    if limit:
        query += f" LIMIT {limit}"
    with _engine.connect() as conn:
        rows = conn.execute(text(query)).fetchall()
    return [r[0] for r in rows]


def _bulk_load(df: pd.DataFrame) -> int:
    with _engine.connect() as conn:
        conn.execute(text(f"TRUNCATE TABLE {_TABLE}"))
        conn.commit()
    df.to_sql(_TABLE, _engine, if_exists="append", index=False, chunksize=5_000, method="multi")
    return len(df)


def _refresh_view() -> None:
    with _engine.connect() as conn:
        try:
            conn.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {_VIEW}"))
            conn.commit()
            log.info("malzeme.view_refreshed", view=_VIEW)
        except Exception as exc:
            log.warning("malzeme.view_refresh_failed", view=_VIEW, error=str(exc))


def run(limit: Optional[int] = None) -> Dict[str, Any]:
    urun_kodlari = _load_urun_kodlari(limit)
    log.info("malzeme.sync_started", toplam_urun=len(urun_kodlari))

    all_rows: List[Dict] = []
    ok = 0
    hata = 0

    with httpx.Client(timeout=30) as client:
        token = _get_token_sync(client)

        for i, urun_kodu in enumerate(urun_kodlari, 1):
            # Her 50'de token taze mi kontrol et
            if i % 50 == 0:
                token = _get_token_sync(client)
                log.info("malzeme.progress", islem=i, toplam=len(urun_kodlari), satirlar=len(all_rows))

            data = _fetch_malzeme(client, token, urun_kodu)
            if not data:
                hata += 1
                continue

            raw_rows = _parse_raw_materials(urun_kodu, data.get("rawMaterials", []))
            aux_rows = _parse_aux_materials(urun_kodu, data.get("auxiliaryMaterials", []))

            if raw_rows or aux_rows:
                all_rows.extend(raw_rows)
                all_rows.extend(aux_rows)
                ok += 1
            else:
                hata += 1

    if not all_rows:
        log.warning("malzeme.no_data")
        return {"ok": 0, "hata": hata, "satirlar": 0}

    df = pd.DataFrame(all_rows)
    # PK dedup — (urun_kodu, malzeme_tipi, malzeme_grup_kodu)
    df = df.drop_duplicates(subset=["urun_kodu", "malzeme_tipi", "malzeme_grup_kodu"], keep="last")

    rows_loaded = _bulk_load(df)
    _refresh_view()

    log.info("malzeme.sync_done", ok=ok, hata=hata, satirlar=rows_loaded)
    return {"ok": ok, "hata": hata, "satirlar": rows_loaded}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Ürün malzeme detaylarını senkronize et.")
    parser.add_argument("--limit", type=int, default=None, help="Test için kaç ürün (varsayılan: hepsi)")
    args = parser.parse_args()

    result = run(limit=args.limit)
    print(f"Tamamlandı: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
