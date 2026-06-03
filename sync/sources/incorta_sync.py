"""Incorta MCP → RDS delta sync.

Strateji: Son N ay için DELETE + INSERT (tam yenileme).
Pagination: pageSize=5000, startRow artırarak tüm satırlar çekilir.
Auth: Authorization header (Bearer token), auth endpoint yok.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import httpx
import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

_MCP_BASE = "https://agentup-mcp-test.pimland.com/30002"
_PAGE_SIZE = 5000

# Incorta araç ID'leri
_TOOLS = {
    "incorta_satis":         "post_api_v2_adl_dashboards_17589647_e5d1_40f7_81ce_a6ec_be9a751b",
    "incorta_depo_iade":     "post_api_v2_adl_dashboards_17589647_e5d1_40f7_81ce_a6ec_94f3f1b2",
    "incorta_iptal_siparis": "post_api_v2_adl_dashboards_17589647_e5d1_40f7_81ce_a6ec_091c37f6",
    "incorta_analytics":     "post_api_v2_adl_dashboards_17589647_e5d1_40f7_81ce_a6ec_3961ae53",
}

# Incorta alan adları (prompts filtresi için)
_YEAR_FIELD    = "CALCULATIONS.EticaretOzet.Year"
_MONTH_FIELD   = "CALCULATIONS.EticaretOzet.Month"
_DATE_FIELD    = "E_Commerce.GA4_Oturum_Kampanyası_Raporu.date"


def _last_n_months(n: int) -> List[Tuple[int, int]]:
    """Son N ayın (yil, ay) listesini döndür."""
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return months


def _fetch_incorta_pages(
    tool: str,
    token: str,
    prompts: List[Dict],
) -> List[List]:
    """Tüm sayfaları çekip ham satır listesi döndür."""
    headers = {"Authorization": token}
    rows: List[List] = []
    start_row = 0

    with httpx.Client(timeout=120) as client:
        while True:
            payload = {
                "Authorization": token,
                "pagination": {"startRow": start_row, "pageSize": _PAGE_SIZE},
                "prompts": prompts,
            }
            resp = client.post(
                f"{_MCP_BASE}/tools/{tool}/execute",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json().get("content", {}).get("data", {})
            page_rows = data.get("data", [])

            if not page_rows:
                break

            rows.extend(page_rows)
            total = data.get("headers", {}).get("totalRows", 0)
            start_row += _PAGE_SIZE
            log.debug("incorta_fetch page=%d rows=%d/%d", start_row // _PAGE_SIZE, len(rows), total)

            if start_row >= (total or 0):
                break

    return rows


def _build_year_month_prompts(months: List[Tuple[int, int]]) -> List[Dict]:
    """(yil, ay) listesinden Incorta prompts filtresi oluştur."""
    years  = list({y for y, _ in months})
    months_ = list({m for _, m in months})
    return [
        {"field": _YEAR_FIELD,  "operator": "in", "values": years,   "type": "integer"},
        {"field": _MONTH_FIELD, "operator": "in", "values": months_, "type": "integer"},
    ]


def _build_date_prompts(months: List[Tuple[int, int]]) -> List[Dict]:
    """Analytics için tarih aralığı filtresi."""
    dates = []
    for y, m in months:
        import calendar
        _, last_day = calendar.monthrange(y, m)
        dates.append(f"{y}-{m:02d}-01")
        dates.append(f"{y}-{m:02d}-{last_day:02d}")
    start = min(dates)
    end   = max(dates)
    return [
        {"field": _DATE_FIELD, "operator": "between", "values": [start, end], "type": "string"},
    ]


# ── incorta_satis ─────────────────────────────────────────────────────────────

def sync_incorta_satis(
    db_conn: "psycopg2.connection",
    incorta_token: str,
    months: int = 3,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    target_months = _last_n_months(months)
    prompts = _build_year_month_prompts(target_months)

    log.info("incorta_satis sync başlıyor months=%d", months)
    rows = _fetch_incorta_pages(_TOOLS["incorta_satis"], incorta_token, prompts)

    with db_conn.cursor() as cur:
        # Hedef dönemleri sil
        for y, m in target_months:
            cur.execute("DELETE FROM incorta_satis WHERE yil=%s AND ay=%s", (y, m))
        silinen = sum(cur.rowcount for _ in target_months)  # yaklaşık

        # Toplu insert
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO incorta_satis
               (yil, ay, satis_kanali, urun_kodu, urun_adi, renk, beden, tutar, adet)
               VALUES %s""",
            [
                (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8])
                for r in rows
            ],
            page_size=1000,
        )
    db_conn.commit()

    sure = round(time.perf_counter() - t0, 1)
    log.info("incorta_satis sync bitti eklenen=%d sure=%.1fs", len(rows), sure)
    return {"tablo": "incorta_satis", "silinen": len(rows), "eklenen": len(rows), "sure_sn": sure}


# ── incorta_depo_iade ─────────────────────────────────────────────────────────

def sync_incorta_depo_iade(
    db_conn: "psycopg2.connection",
    incorta_token: str,
    months: int = 3,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    target_months = _last_n_months(months)
    prompts = _build_year_month_prompts(target_months)

    log.info("incorta_depo_iade sync başlıyor months=%d", months)
    rows = _fetch_incorta_pages(_TOOLS["incorta_depo_iade"], incorta_token, prompts)

    with db_conn.cursor() as cur:
        for y, m in target_months:
            cur.execute("DELETE FROM incorta_depo_iade WHERE yil=%s AND ay=%s", (y, m))

        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO incorta_depo_iade
               (yil, ay, satis_kanali, urun_kodu, urun_adi, renk, beden, tutar, adet)
               VALUES %s""",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows],
            page_size=1000,
        )
    db_conn.commit()

    sure = round(time.perf_counter() - t0, 1)
    log.info("incorta_depo_iade sync bitti eklenen=%d sure=%.1fs", len(rows), sure)
    return {"tablo": "incorta_depo_iade", "silinen": len(rows), "eklenen": len(rows), "sure_sn": sure}


# ── incorta_iptal_siparis ─────────────────────────────────────────────────────

def sync_incorta_iptal_siparis(
    db_conn: "psycopg2.connection",
    incorta_token: str,
    months: int = 3,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    target_months = _last_n_months(months)
    prompts = _build_year_month_prompts(target_months)

    log.info("incorta_iptal_siparis sync başlıyor months=%d", months)
    rows = _fetch_incorta_pages(_TOOLS["incorta_iptal_siparis"], incorta_token, prompts)

    with db_conn.cursor() as cur:
        for y, m in target_months:
            cur.execute("DELETE FROM incorta_iptal_siparis WHERE yil=%s AND ay=%s", (y, m))

        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO incorta_iptal_siparis
               (yil, ay, satis_kanali, urun_kodu, urun_adi, renk, beden, tutar, adet)
               VALUES %s""",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows],
            page_size=1000,
        )
    db_conn.commit()

    sure = round(time.perf_counter() - t0, 1)
    log.info("incorta_iptal_siparis sync bitti eklenen=%d sure=%.1fs", len(rows), sure)
    return {"tablo": "incorta_iptal_siparis", "silinen": len(rows), "eklenen": len(rows), "sure_sn": sure}


# ── incorta_analytics ─────────────────────────────────────────────────────────

def sync_incorta_analytics(
    db_conn: "psycopg2.connection",
    incorta_token: str,
    months: int = 3,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    target_months = _last_n_months(months)
    prompts = _build_date_prompts(target_months)

    log.info("incorta_analytics sync başlıyor months=%d", months)
    rows = _fetch_incorta_pages(_TOOLS["incorta_analytics"], incorta_token, prompts)

    # Tarih aralığını hesapla
    import calendar
    min_date = min(f"{y}-{m:02d}-01" for y, m in target_months)
    max_date = max(
        f"{y}-{m:02d}-{calendar.monthrange(y,m)[1]:02d}"
        for y, m in target_months
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM incorta_analytics WHERE date >= %s AND date <= %s",
            (min_date, max_date),
        )

        # Production'da kolon adı i_slem_sayisi
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO incorta_analytics
               (date, marka, oturum_kaynagi, oturum_kampanyasi,
                kullanicilar, oturumlar, ciro, i_slem_sayisi,
                conversion_rate, hemen_cikma_orani, ortalama_oturum_suresi)
               VALUES %s
               ON CONFLICT DO NOTHING""",
            [
                (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10])
                for r in rows
            ],
            page_size=1000,
        )
    db_conn.commit()

    sure = round(time.perf_counter() - t0, 1)
    log.info("incorta_analytics sync bitti eklenen=%d sure=%.1fs", len(rows), sure)
    return {"tablo": "incorta_analytics", "silinen": len(rows), "eklenen": len(rows), "sure_sn": sure}
