"""Pimland MCP → RDS sync.

Master data: Haftalık full replace (TRUNCATE + INSERT).
Ürün kataloğu: Günlük delta (updatedDate parametresi ile UPSERT).
Auth: OAuth2 password grant (pimland_live.py ile aynı pattern).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

_MCP_BASE  = "https://agentup-mcp-test.pimland.com/30001"
_TOKEN_URL = os.environ.get("PIMLAND_TOKEN_URL", "https://ids.pimland.com/connect/token")

# Master data araç adları
_MASTER_TOOLS = {
    "pim_brands":         "post_api_MasterData_brands_get_brands",
    "pim_seasons":        "post_api_MasterData_seasons_get_seasons",
    "pim_themes":         "post_api_MasterData_product_themes_get_product_themes",
    "pim_main_groups":    "post_api_MasterData_main_product_groups_get_main_product_groups",
    "pim_product_groups": "post_api_MasterData_product_groups_get_product_groups",
    "pim_colors":         "post_api_MasterData_colors_get_colors",
}

# CDN base URL
_CDN_BASE = "https://img-adl.sm.mncdn.com/cdnimages/products"


# ── OAuth2 token ──────────────────────────────────────────────────────────────

_token_cache: Dict[str, Any] = {}


def _get_token(client: httpx.Client) -> str:
    if _token_cache.get("expires_at", 0) > time.time() + 30:
        return _token_cache["access_token"]

    resp = client.post(_TOKEN_URL, data={
        "grant_type":    "password",
        "client_id":     os.environ.get("PIMLAND_CLIENT_ID", ""),
        "client_secret": os.environ.get("PIMLAND_CLIENT_SECRET", ""),
        "username":      os.environ.get("PIMLAND_USERNAME", ""),
        "password":      os.environ.get("PIMLAND_PASSWORD", ""),
        "scope":         os.environ.get("PIMLAND_SCOPE", ""),
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache.update({
        "access_token": data["access_token"],
        "expires_at":   time.time() + data.get("expires_in", 3600),
    })
    return data["access_token"]


def _call_tool(client: httpx.Client, token: str, tool: str, args: Dict) -> Any:
    h = {"Authorization": f"Bearer {token}"}
    resp = client.post(f"{_MCP_BASE}/tools/{tool}/execute", headers=h, json=args, timeout=60)
    resp.raise_for_status()
    d = resp.json()
    return (
        d.get("content", {}).get("data", {}).get("result")
        or d.get("content", {}).get("data")
        or d
    )


def _listify(raw: Any) -> List[Dict]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    for k in ("items", "data", "result", "brands", "colors", "seasons",
               "mainProductGroups", "productGroups", "productThemes", "products"):
        v = raw.get(k) if isinstance(raw, dict) else None
        if isinstance(v, list):
            return v
    return []


# ── Master data sync ──────────────────────────────────────────────────────────

_MASTER_TABLES = {
    "pim_brands": {
        "tool":    "post_api_MasterData_brands_get_brands",
        "table":   "pim_brands",
        "columns": ["brand_code", "brand_name", "is_active"],
        "map":     lambda r: (r.get("referenceCode"), r.get("name"), r.get("isActive", True)),
    },
    "pim_seasons": {
        "tool":    "post_api_MasterData_seasons_get_seasons",
        "table":   "pim_seasons",
        "columns": ["season_code", "season_name", "is_active"],
        "map":     lambda r: (r.get("referenceCode"), r.get("name"), r.get("isActive", True)),
    },
    "pim_themes": {
        "tool":    "post_api_MasterData_product_themes_get_product_themes",
        "table":   "pim_themes",
        "columns": ["theme_code", "theme_name", "is_active"],
        "map":     lambda r: (r.get("referenceCode"), r.get("name"), r.get("isActive", True)),
    },
    "pim_main_groups": {
        "tool":    "post_api_MasterData_main_product_groups_get_main_product_groups",
        "table":   "pim_main_groups",
        "columns": ["main_group_code", "main_group_name", "is_active"],
        "map":     lambda r: (r.get("referenceCode"), r.get("name"), r.get("isActive", True)),
    },
    "pim_product_groups": {
        "tool":    "post_api_MasterData_product_groups_get_product_groups",
        "table":   "pim_product_groups",
        "columns": ["group_code", "group_name", "is_active"],
        "map":     lambda r: (r.get("referenceCode"), r.get("name"), r.get("isActive", True)),
    },
    "pim_colors": {
        "tool":    "post_api_MasterData_colors_get_colors",
        "table":   "pim_colors",
        "columns": ["color_code", "color_name", "hex_code", "is_active"],
        "map":     lambda r: (r.get("referenceCode"), r.get("name"), None, r.get("isActive", True)),
    },
}


def sync_master_data(db_conn: "psycopg2.connection") -> List[Dict[str, Any]]:
    """Master data tablolarını full replace ile güncelle."""
    results = []
    with httpx.Client(timeout=60) as client:
        token = _get_token(client)

        for key, cfg in _MASTER_TABLES.items():
            t0 = time.perf_counter()
            log.info("master_data sync: %s", key)

            raw = _call_tool(client, token, cfg["tool"], {"filter": None})
            records = _listify(raw)

            if not records:
                log.warning("master_data boş döndü: %s", key)
                results.append({"tablo": key, "eklenen": 0, "guncellenen": 0, "sure_sn": 0})
                continue

            rows = [cfg["map"](r) for r in records if r]

            with db_conn.cursor() as cur:
                # Tablonun var olup olmadığını kontrol et — yoksa atla
                cur.execute(
                    "SELECT to_regclass(%s::text) IS NOT NULL",
                    (cfg["table"],)
                )
                if not cur.fetchone()[0]:
                    log.info("master_data tablo yok, atlanıyor: %s", cfg["table"])
                    continue

                cur.execute(f"TRUNCATE TABLE {cfg['table']}")
                cols = ", ".join(cfg["columns"])
                placeholders = ", ".join(["%s"] * len(cfg["columns"]))
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO {cfg['table']} ({cols}) VALUES %s",
                    rows,
                    page_size=500,
                )
            db_conn.commit()

            sure = round(time.perf_counter() - t0, 1)
            log.info("master_data sync bitti: %s eklenen=%d sure=%.1fs", key, len(rows), sure)
            results.append({"tablo": key, "eklenen": len(rows), "guncellenen": 0, "sure_sn": sure})

    return results


# ── Generic master data sync (config-driven) ──────────────────────────────────

def sync_master_table(
    db_conn: "psycopg2.connection",
    table_cfg: Dict[str, Any],
    creds: Dict[str, str],
) -> Dict[str, Any]:
    """
    sources.yaml'daki tek bir master data tablosunu full replace ile güncelle.

    table_cfg yapısı:
        tool, rds_table, pk, columns [{source, target, type}]
    creds:
        token_url, client_id, client_secret, username, password, scope
    """
    t0 = time.perf_counter()
    tool      = table_cfg["tool"]
    rds_table = table_cfg["rds_table"]
    columns   = table_cfg["columns"]

    # Token al
    with httpx.Client(timeout=30) as client:
        resp = client.post(creds["token_url"], data={
            "grant_type":    "password",
            "client_id":     creds["client_id"],
            "client_secret": creds["client_secret"],
            "username":      creds["username"],
            "password":      creds["password"],
            "scope":         creds["scope"],
        })
        resp.raise_for_status()
        token = resp.json()["access_token"]

        raw = _call_tool(client, token, tool, {"filter": None})

    records = _listify(raw)
    if not records:
        log.warning("sync_master_table boş döndü: %s", rds_table)
        return {"tablo": rds_table, "eklenen": 0, "guncellenen": 0, "sure_sn": 0}

    # source alanından hedef tuple oluştur
    col_targets = [c["target"] for c in columns]
    col_sources = [c["source"] for c in columns]
    rows = [
        tuple(r.get(src) for src in col_sources)
        for r in records if r
    ]

    with db_conn.cursor() as cur:
        # Tablo var mı?
        cur.execute("SELECT to_regclass(%s::text) IS NOT NULL", (rds_table,))
        if not cur.fetchone()[0]:
            log.warning("sync_master_table tablo yok, atlanıyor: %s", rds_table)
            return {"tablo": rds_table, "eklenen": 0, "guncellenen": 0, "sure_sn": 0}

        cur.execute(f"TRUNCATE TABLE {rds_table}")
        cols_sql = ", ".join(col_targets)
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {rds_table} ({cols_sql}) VALUES %s",
            rows,
            page_size=500,
        )
    db_conn.commit()

    sure = round(time.perf_counter() - t0, 1)
    log.info("sync_master_table bitti: %s eklenen=%d sure=%.1fs", rds_table, len(rows), sure)
    return {"tablo": rds_table, "eklenen": len(rows), "guncellenen": 0, "sure_sn": sure}


# ── Ürün kataloğu delta sync ──────────────────────────────────────────────────

def _extract_image_url(product: Dict) -> Optional[str]:
    images = product.get("productImages") or []
    if images:
        img = images[0]
        name = img.get("name", "")
        if name:
            return f"{_CDN_BASE}/{name}"
    # Fallback: stockCode + first color
    barcodes = product.get("barcodes") or []
    if barcodes:
        color = barcodes[0].get("colorCode", "")
        sku   = product.get("stockCode", "")
        if sku and color:
            return f"{_CDN_BASE}/{sku}_{color}_1.jpg"
    return None


def _extract_main_material(product: Dict) -> Optional[str]:
    barcodes = product.get("barcodes") or []
    for bc in barcodes:
        content = bc.get("mainMaterialContent")
        if content:
            return content
    return None


def _extract_color_codes(product: Dict) -> str:
    barcodes = product.get("barcodes") or []
    codes = list(dict.fromkeys(bc.get("colorCode", "") for bc in barcodes if bc.get("colorCode")))
    return ",".join(codes)


def sync_products(
    db_conn: "psycopg2.connection",
    last_sync_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Pimland ürün kataloğunu delta sync ile güncelle."""
    t0 = time.perf_counter()

    if last_sync_date is None:
        updated_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    else:
        updated_date = (last_sync_date - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

    log.info("pimland_products sync başlıyor updated_since=%s", updated_date)

    eklenen = guncellenen = 0
    page = 1

    with httpx.Client(timeout=60) as client:
        token = _get_token(client)

        while True:
            raw = _call_tool(client, token, "post_api_Product_get_products_with_squ", {
                "updatedDate": updated_date,
                "pageSize":    100,
                "pageNumber":  page,
            })

            products = []
            if isinstance(raw, dict):
                products = raw.get("products") or _listify(raw)
                total_pages = raw.get("totalPageCount", 1)
            else:
                products = _listify(raw)
                total_pages = 1

            if not products:
                break

            log.debug("pimland_products page=%d count=%d", page, len(products))

            rows = []
            for p in products:
                barcodes = p.get("barcodes") or []
                color_codes = _extract_color_codes(p)
                first_color = (barcodes[0].get("colorCode", "") if barcodes else "")

                rows.append((
                    p.get("stockCode"),
                    p.get("description"),
                    p.get("seasonCode"),
                    p.get("seasonName"),
                    p.get("brandCode"),
                    p.get("brandName"),
                    p.get("productGroupCode"),
                    p.get("productGroupName"),
                    p.get("productMainGroupCode"),
                    p.get("productMainGroupName"),
                    p.get("productThemeCode"),
                    p.get("productThemeName"),
                    p.get("fabricMaterialCode"),
                    p.get("fabricMaterialName"),
                    p.get("isBlocked", False),
                    p.get("useInternet", False),
                    color_codes,
                    first_color,
                    _extract_image_url(p),
                    datetime.utcnow(),
                ))

            with db_conn.cursor() as cur:
                result = psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO pim_products (
                        urun_kodu, urun_adi, sezon_kodu, sezon_adi,
                        marka_kodu, marka_adi, urun_grubu_kodu, urun_grubu_adi,
                        ana_grup_kodu, ana_grup_adi, tema_kodu, tema_adi,
                        fabricmaterialcode, fabricmaterialname,
                        bloke, internet_aktif,
                        color_codes, first_color_code, default_image_url,
                        sync_updated_at
                    ) VALUES %s
                    ON CONFLICT (urun_kodu) DO UPDATE SET
                        urun_adi          = EXCLUDED.urun_adi,
                        sezon_kodu        = EXCLUDED.sezon_kodu,
                        sezon_adi         = EXCLUDED.sezon_adi,
                        marka_kodu        = EXCLUDED.marka_kodu,
                        marka_adi         = EXCLUDED.marka_adi,
                        urun_grubu_kodu   = EXCLUDED.urun_grubu_kodu,
                        urun_grubu_adi    = EXCLUDED.urun_grubu_adi,
                        ana_grup_kodu     = EXCLUDED.ana_grup_kodu,
                        ana_grup_adi      = EXCLUDED.ana_grup_adi,
                        tema_kodu         = EXCLUDED.tema_kodu,
                        tema_adi          = EXCLUDED.tema_adi,
                        fabricmaterialcode = EXCLUDED.fabricmaterialcode,
                        fabricmaterialname = EXCLUDED.fabricmaterialname,
                        bloke             = EXCLUDED.bloke,
                        internet_aktif    = EXCLUDED.internet_aktif,
                        color_codes       = EXCLUDED.color_codes,
                        first_color_code  = EXCLUDED.first_color_code,
                        default_image_url = EXCLUDED.default_image_url,
                        sync_updated_at   = EXCLUDED.sync_updated_at
                    """,
                    rows,
                    fetch=True,
                    page_size=100,
                )
            db_conn.commit()
            eklenen += len(rows)

            if page >= total_pages:
                break
            page += 1

    sure = round(time.perf_counter() - t0, 1)
    log.info("pimland_products sync bitti eklenen/guncellenen=%d sure=%.1fs", eklenen, sure)
    return {
        "tablo": "pim_products",
        "eklenen": eklenen,
        "guncellenen": guncellenen,
        "sure_sn": sure,
    }
