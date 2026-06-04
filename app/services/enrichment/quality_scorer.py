"""Ürün kalite puanlama motoru.

Toplam 100 puan, 4 kategori × 25 puan:
  - Temel Bilgi   (25): description, brand, season, group
  - Kumaş Bilgisi (25): fabricMaterial, mainMaterialContent, care, fit
  - Görsel        (25): productImages sayısı + renk çeşitliliği + hex
  - Satış İçerik  (25): description uzunluğu, ecomTag, theme, notes
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

import psycopg2
import psycopg2.extras

from app.core.logging import get_logger

log = get_logger(__name__)


# ── Grade sınırları ───────────────────────────────────────────────────────────

def calc_grade(score: int) -> str:
    if score >= 90: return "A"
    if score >= 75: return "B"
    if score >= 60: return "C"
    if score >= 40: return "D"
    return "F"


# ── Tek ürün puanlama ─────────────────────────────────────────────────────────

def score_product(stock_code: str, product_data: dict) -> dict:
    """
    Tek ürünü puanla. Senkron fonksiyon.
    product_data: get_products_with_squ'dan gelen veri.
    """
    eksik: List[str] = []
    hatali: List[Dict] = []
    uyarilar: List[str] = []

    barcodes = product_data.get("barcodes") or []
    images   = product_data.get("productImages") or []
    care     = product_data.get("washingAndCareInstructions") or []
    desc     = (product_data.get("description") or "").strip()

    # ── Temel Bilgi (25) ─────────────────────────────────────────────────────
    t = 0
    if desc and len(desc) >= 10:
        t += 8
    elif desc:
        t += 3
        hatali.append({"alan": "description", "sorun": f"çok kısa ({len(desc)} karakter, min 10)"})
    else:
        eksik.append("description")

    if product_data.get("brandCode"):
        t += 4
    else:
        eksik.append("brandCode")

    if product_data.get("seasonCode"):
        t += 4
    else:
        eksik.append("seasonCode")

    if product_data.get("productGroupCode"):
        t += 5
    else:
        eksik.append("productGroupCode")

    if product_data.get("productMainGroupCode"):
        t += 4
    else:
        eksik.append("productMainGroupCode")

    # ── Kumaş Bilgisi (25) ───────────────────────────────────────────────────
    k = 0
    if product_data.get("fabricMaterialName"):
        k += 8
    else:
        eksik.append("fabricMaterialName")

    # mainMaterialContent: barcodes içinde herhangi birinde olmalı
    mat_contents = [b.get("mainMaterialContent", "") for b in barcodes if b.get("mainMaterialContent")]
    if mat_contents:
        # En az birinde % işareti olmalı
        if any("%" in m for m in mat_contents):
            k += 8
        else:
            k += 4
            hatali.append({"alan": "mainMaterialContent", "sorun": "% içeriği bulunamadı"})
    else:
        eksik.append("mainMaterialContent")

    if care and len(care) >= 1:
        k += 6
    else:
        eksik.append("washingAndCareInstructions")

    if product_data.get("fitCode"):
        k += 3
    else:
        uyarilar.append("fitCode atanmamış")

    # ── Görsel (25) ──────────────────────────────────────────────────────────
    g = 0
    img_count = len(images)
    if img_count >= 3:
        g += 15
    elif img_count == 2:
        g += 10
    elif img_count == 1:
        g += 5
    else:
        eksik.append("productImages")

    # Renk çeşitliliği: her renk için en az 1 görsel
    if images and barcodes:
        renk_kodlari = set(b.get("colorCode", "") for b in barcodes if b.get("colorCode"))
        gorsel_renkleri = set(img.get("colorCode", "") for img in images if img.get("colorCode"))
        if renk_kodlari and renk_kodlari.issubset(gorsel_renkleri):
            g += 8
        elif gorsel_renkleri:
            g += 4
            eksik_renkler = renk_kodlari - gorsel_renkleri
            if eksik_renkler:
                uyarilar.append(f"{len(eksik_renkler)} renk için görsel eksik")
        else:
            uyarilar.append("Görsellerde renk kodu tanımlanmamış")
    elif img_count > 0:
        g += 4

    # colorHexCode
    if any(b.get("colorHexCode") for b in barcodes):
        g += 2
    else:
        uyarilar.append("Renk hex kodu tanımlanmamış")

    # ── Satış İçerik (25) ────────────────────────────────────────────────────
    s = 0
    if len(desc) >= 50:
        s += 10
    elif len(desc) >= 10:
        s += 5
        hatali.append({"alan": "description (satış)", "sorun": f"kısa ({len(desc)} karakter, ideal ≥50)"})

    if product_data.get("ecomTag1Code"):
        s += 5
    else:
        eksik.append("ecomTag1Code")

    if product_data.get("productThemeCode"):
        s += 5
    else:
        uyarilar.append("Tema atanmamış")

    if (product_data.get("notes") or "").strip():
        s += 5
    else:
        eksik.append("notes")

    total = t + k + g + s
    grade = calc_grade(total)

    return {
        "urun_kodu":          stock_code,
        "sezon_kodu":         product_data.get("seasonCode"),
        "sezon_adi":          product_data.get("seasonName"),
        "quality_score":      total,
        "quality_grade":      grade,
        "score_temel_bilgi":  t,
        "score_kumas_bilgi":  k,
        "score_gorsel":       g,
        "score_satis_icerik": s,
        "eksik_alanlar":      eksik,
        "hatali_alanlar":     hatali,
        "uyarilar":           uyarilar,
    }


# ── Sezon toplu puanlama ──────────────────────────────────────────────────────

async def score_season(
    season_code: str,
    db_conn: "psycopg2.connection",
    progress_callback: Optional[Callable] = None,
) -> dict:
    """
    Sezonun tüm ürünlerini pim_products'tan okuyup puanlar.
    MCP down olsa bile DB verisini kullanır.
    enrichment_quality tablosuna UPSERT yapar.
    enrichment_season_summary günceller.
    """
    from sqlalchemy import create_engine, text as sa_text
    import os

    started = time.perf_counter()

    # pim_products'tan sezon ürünlerini çek
    with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT urun_kodu, urun_adi, sezon_kodu, sezon_adi,
                   marka_kodu, marka_adi, urun_grubu_kodu, urun_grubu_adi,
                   ana_grup_kodu, ana_grup_adi, tema_kodu, tema_adi,
                   fabricmaterialname, bloke, internet_aktif,
                   color_codes, first_color_code, default_image_url
            FROM pim_products
            WHERE sezon_kodu = %s
            ORDER BY urun_kodu
        """, (season_code,))
        db_rows = cur.fetchall()

    total = len(db_rows)
    if total == 0:
        return {"error": f"'{season_code}' sezonu için ürün bulunamadı"}

    log.info("quality_scorer.season_start", season=season_code, total=total)

    results = []
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    sorun_sayac: Dict[str, int] = {}

    for i, row in enumerate(db_rows):
        # DB verisini get_products_with_squ formatına uyarla
        product_data = {
            "stockCode":           row["urun_kodu"],
            "description":         row["urun_adi"] or "",
            "brandCode":           row["marka_kodu"],
            "brandName":           row["marka_adi"],
            "seasonCode":          row["sezon_kodu"],
            "seasonName":          row["sezon_adi"],
            "productGroupCode":    row["urun_grubu_kodu"],
            "productMainGroupCode":row["ana_grup_kodu"],
            "productThemeCode":    row["tema_kodu"],
            "fabricMaterialName":  row["fabricmaterialname"],
            "fitCode":             None,
            "ecomTag1Code":        None,
            "notes":               None,
            # Görsel: default_image_url varsa 1 görsel var sayıyoruz
            "productImages":       [{"colorCode": row["first_color_code"]}]
                                   if row.get("default_image_url") else [],
            # Barcodes: color_codes'tan renk kodlarını çıkar
            "barcodes":            [
                {"colorCode": c.strip(), "mainMaterialContent": "", "colorHexCode": None}
                for c in (row.get("color_codes") or "").split(",")
                if c.strip()
            ],
            "washingAndCareInstructions": [],
        }

        result = score_product(row["urun_kodu"], product_data)
        results.append(result)

        grade_counts[result["quality_grade"]] = grade_counts.get(result["quality_grade"], 0) + 1
        for eksik in result["eksik_alanlar"]:
            sorun_sayac[eksik] = sorun_sayac.get(eksik, 0) + 1

        if progress_callback and (i + 1) % 10 == 0:
            progress_callback(i + 1, total)

    # Toplu UPSERT
    import json
    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO enrichment_quality (
                urun_kodu, sezon_kodu, sezon_adi,
                quality_score, quality_grade,
                score_temel_bilgi, score_kumas_bilgi,
                score_gorsel, score_satis_icerik,
                eksik_alanlar, hatali_alanlar, uyarilar
            ) VALUES %s
            ON CONFLICT (urun_kodu) DO UPDATE SET
                sezon_kodu        = EXCLUDED.sezon_kodu,
                sezon_adi         = EXCLUDED.sezon_adi,
                quality_score     = EXCLUDED.quality_score,
                quality_grade     = EXCLUDED.quality_grade,
                score_temel_bilgi = EXCLUDED.score_temel_bilgi,
                score_kumas_bilgi = EXCLUDED.score_kumas_bilgi,
                score_gorsel      = EXCLUDED.score_gorsel,
                score_satis_icerik= EXCLUDED.score_satis_icerik,
                eksik_alanlar     = EXCLUDED.eksik_alanlar,
                hatali_alanlar    = EXCLUDED.hatali_alanlar,
                uyarilar          = EXCLUDED.uyarilar,
                last_scored_at    = NOW()
            """,
            [
                (
                    r["urun_kodu"], r["sezon_kodu"], r["sezon_adi"],
                    r["quality_score"], r["quality_grade"],
                    r["score_temel_bilgi"], r["score_kumas_bilgi"],
                    r["score_gorsel"], r["score_satis_icerik"],
                    json.dumps(r["eksik_alanlar"], ensure_ascii=False),
                    json.dumps(r["hatali_alanlar"], ensure_ascii=False),
                    json.dumps(r["uyarilar"], ensure_ascii=False),
                )
                for r in results
            ],
            page_size=200,
        )

        # Season summary güncelle
        ort_puan = round(sum(r["quality_score"] for r in results) / total, 1) if total else 0
        top_sorunlar = sorted(
            [{"sorun": k, "sayi": v, "pct": round(v / total * 100)} for k, v in sorun_sayac.items()],
            key=lambda x: -x["sayi"]
        )[:10]

        season_name = db_rows[0]["sezon_adi"] if db_rows else season_code
        cur.execute("""
            INSERT INTO enrichment_season_summary
                (sezon_kodu, sezon_adi, toplam_urun, ortalama_puan,
                 grade_a, grade_b, grade_c, grade_d, grade_f,
                 top_sorunlar, scored_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (sezon_kodu) DO UPDATE SET
                sezon_adi    = EXCLUDED.sezon_adi,
                toplam_urun  = EXCLUDED.toplam_urun,
                ortalama_puan= EXCLUDED.ortalama_puan,
                grade_a=EXCLUDED.grade_a, grade_b=EXCLUDED.grade_b,
                grade_c=EXCLUDED.grade_c, grade_d=EXCLUDED.grade_d,
                grade_f=EXCLUDED.grade_f,
                top_sorunlar = EXCLUDED.top_sorunlar,
                scored_at    = NOW()
        """, (
            season_code, season_name, total, ort_puan,
            grade_counts["A"], grade_counts["B"], grade_counts["C"],
            grade_counts["D"], grade_counts["F"],
            json.dumps(top_sorunlar, ensure_ascii=False),
        ))

    db_conn.commit()

    sure = round(time.perf_counter() - started, 1)
    log.info("quality_scorer.season_done",
             season=season_code, total=total, avg=ort_puan, sure_sn=sure)

    if progress_callback:
        progress_callback(total, total)

    return {
        "season_code":   season_code,
        "toplam_urun":   total,
        "ortalama_puan": ort_puan,
        "grade_counts":  grade_counts,
        "sure_sn":       sure,
    }
