"""Incorta MCP → RDS delta sync.

Config kaynağı: sync/config/sources.yaml
generic_incorta_sync() tek fonksiyon — tüm tablolar için çalışır.
Yeni tablo eklemek: sadece sources.yaml'a giriş ekle.
"""

from __future__ import annotations

import calendar
import logging
import time
from datetime import date
from typing import Any, Dict, List, Tuple

import httpx
import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)
_PAGE_SIZE = 5000


# ── Yardımcı fonksiyonlar ─────────────────────────────────────────────────────

def _last_n_months(n: int) -> List[Tuple[int, int]]:
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return months


def _build_year_month_prompts(months: List[Tuple[int, int]], cfg: Dict) -> List[Dict]:
    years  = list({y for y, _ in months})
    mths   = list({m for _, m in months})
    return [
        {"field": cfg["year_field"],  "operator": "IN", "values": years, "type": "dimension"},
        {"field": cfg["month_field"], "operator": "IN", "values": mths,  "type": "dimension"},
    ]


def _build_date_prompts(months: List[Tuple[int, int]], cfg: Dict) -> List[Dict]:
    start = min(f"{y}-{m:02d}-01" for y, m in months)
    end   = max(f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}" for y, m in months)
    return [
        {"field": cfg["date_field"], "operator": "BETWEEN", "values": [start, end], "type": "dimension"},
    ]


def _fetch_pages(tool: str, token: str, prompts: List[Dict], page_size: int) -> List[List]:
    rows: List[List] = []
    start_row = 0
    with httpx.Client(timeout=120) as client:
        while True:
            payload = {
                "Authorization": token,
                "pagination": {"startRow": start_row, "pageSize": page_size},
                "prompts": prompts,
            }
            resp = client.post(
                f"https://agentup-mcp-test.pimland.com/30002/tools/{tool}/execute",
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json().get("content", {}).get("data", {})
            page = body.get("data", [])
            if not page:
                break
            rows.extend(page)
            total = body.get("headers", {}).get("totalRows", 0)
            start_row += page_size
            log.debug("incorta fetch tool=%s rows=%d/%d", tool[:30], len(rows), total)
            if start_row >= (total or 0):
                break
    return rows


def _row_to_tuple(row: List, columns: List[Dict]) -> tuple:
    """Ham Incorta satırını (list) hedef kolon sırasına göre tuple'a çevir."""
    return tuple(row[i] if i < len(row) else None for i in range(len(columns)))


# ── Generic sync ──────────────────────────────────────────────────────────────

def generic_incorta_sync(
    db_conn: "psycopg2.connection",
    table_cfg: Dict[str, Any],
    token: str,
) -> Dict[str, Any]:
    """
    sources.yaml'daki tek bir Incorta tablo konfigürasyonunu uygular.

    table_cfg yapısı:
        name, tool, strategy, filter_type,
        year_field/month_field veya date_field,
        rds_table, columns, delete_key, page_size
    """
    t0 = time.perf_counter()
    name       = table_cfg["name"]
    tool       = table_cfg["tool"]
    rds_table  = table_cfg["rds_table"]
    columns    = table_cfg["columns"]
    delete_key = table_cfg["delete_key"]
    page_size  = table_cfg.get("page_size", _PAGE_SIZE)
    months     = 3  # delta_3_month stratejisi

    log.info("generic_incorta_sync başlıyor table=%s", name)

    # Filtre prompts'unu oluştur
    target_months = _last_n_months(months)
    filter_type = table_cfg.get("filter_type", "year_month")
    if filter_type == "year_month":
        prompts = _build_year_month_prompts(target_months, table_cfg)
    else:
        prompts = _build_date_prompts(target_months, table_cfg)

    # MCP'den çek
    rows = _fetch_pages(tool, token, prompts, page_size)

    # Hedef kolonlar ve insert SQL'i
    col_names    = [c["target"] for c in columns]
    cols_sql     = ", ".join(col_names)
    placeholders = ", ".join(["%s"] * len(col_names))

    with db_conn.cursor() as cur:
        # Hedef dönemleri sil
        if filter_type == "year_month":
            for y, m in target_months:
                where_cols = " AND ".join(f"{k}=%s" for k in delete_key)
                cur.execute(f"DELETE FROM {rds_table} WHERE {where_cols}", (y, m))
        else:
            # date_range: tarih aralığını sil
            dates = [f"{y}-{m:02d}-01" for y, m in target_months]
            months_list = ", ".join(["%s"] * len(dates))
            # Ay bazlı silme — date kolonu text ise LIKE, date ise BETWEEN
            for y, m in target_months:
                cur.execute(
                    f"DELETE FROM {rds_table} WHERE {delete_key[0]} LIKE %s",
                    (f"{y}-{m:02d}-%",)
                )

        # Insert
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {rds_table} ({cols_sql}) VALUES %s",
            [_row_to_tuple(r, columns) for r in rows],
            page_size=1000,
        )
    db_conn.commit()

    sure = round(time.perf_counter() - t0, 1)
    log.info("generic_incorta_sync bitti table=%s eklenen=%d sure=%.1fs", name, len(rows), sure)
    return {"tablo": name, "silinen": len(rows), "eklenen": len(rows), "sure_sn": sure}


# ── Geriye dönük uyumluluk wrapper'ları ──────────────────────────────────────
# Eski çağrı noktaları için; yeni kod generic_incorta_sync kullanmalı.

def sync_incorta_satis(db_conn, incorta_token, months=3):
    from sync.config_loader import get_incorta_table
    return generic_incorta_sync(db_conn, get_incorta_table("incorta_satis"), incorta_token)


def sync_incorta_depo_iade(db_conn, incorta_token, months=3):
    from sync.config_loader import get_incorta_table
    return generic_incorta_sync(db_conn, get_incorta_table("incorta_depo_iade"), incorta_token)


def sync_incorta_iptal_siparis(db_conn, incorta_token, months=3):
    from sync.config_loader import get_incorta_table
    return generic_incorta_sync(db_conn, get_incorta_table("incorta_iptal_siparis"), incorta_token)


def sync_incorta_analytics(db_conn, incorta_token, months=3):
    from sync.config_loader import get_incorta_table
    return generic_incorta_sync(db_conn, get_incorta_table("incorta_analytics"), incorta_token)
