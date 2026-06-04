"""Ürün kalite puanlama motoru — Genişletilmiş v2.

Toplam 100 puan, 4 kategori × 25 puan:
  - Temel Bilgi   (25): description, brand, season, group, type, vat
  - Kumaş Bilgisi (25): fabricMaterial, mainMaterialContent (%), care instructions,
                        fit, kompozisyon detay, bakım adedi
  - Görsel        (25): productImages sayısı, renk başına görsel, hex kodu,
                        görsel kalitesi (img type variety)
  - Satış İçerik  (25): description uzunluğu (50+), ecomTags (1-4), theme,
                        notes, productRelations, productStories
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

import psycopg2
import psycopg2.extras

from app.core.logging import get_logger

log = get_logger(__name__)


def calc_grade(score: int) -> str:
    if score >= 90: return "A"
    if score >= 75: return "B"
    if score >= 60: return "C"
    if score >= 40: return "D"
    return "F"


def score_product(stock_code: str, product_data: dict) -> dict:
    """
    Tek ürünü puanla. Senkron fonksiyon.
    product_data: get_products_with_squ'dan gelen veri.
    """
    eksik: List[str] = []
    hatali: List[Dict] = []
    uyarilar: List[str] = []

    barcodes  = product_data.get("barcodes") or []
    images    = product_data.get("productImages") or []
    care      = product_data.get("washingAndCareInstructions") or []
    relations = product_data.get("productRelations") or []
    stories   = product_data.get("productStories") or []
    desc      = (product_data.get("description") or "").strip()
    notes     = (product_data.get("notes") or "").strip()

    # ── Temel Bilgi (25 puan) ────────────────────────────────────────────────
    # description: 5p (var+min10), brandCode: 4p, seasonCode: 3p,
    # productGroupCode: 5p, productMainGroupCode: 4p, productTypeCode: 2p, vatRate: 2p
    t = 0

    if desc and len(desc) >= 10:
        t += 5
    elif desc:
        t += 2
        hatali.append({"alan": "Ürün Açıklaması", "sorun": f"çok kısa ({len(desc)} karakter, min 10)"})
    else:
        eksik.append("Ürün Açıklaması")

    if product_data.get("brandCode"):
        t += 4
    else:
        eksik.append("Marka")

    if product_data.get("seasonCode"):
        t += 3
    else:
        eksik.append("Sezon")

    if product_data.get("productGroupCode"):
        t += 5
    else:
        eksik.append("Ürün Grubu")

    if product_data.get("productMainGroupCode"):
        t += 4
    else:
        eksik.append("Ana Kategori")

    if product_data.get("productTypeCode"):
        t += 2
    else:
        uyarilar.append("Kumaş Tipi (Dokuma/Örme) belirtilmemiş")

    vat = product_data.get("vatRate")
    if vat is not None and vat >= 0:
        t += 2
    else:
        uyarilar.append("KDV Oranı tanımlanmamış")

    # ── Kumaş Bilgisi (25 puan) ──────────────────────────────────────────────
    # fabricMaterialName: 5p, mainMaterialContent+%: 7p,
    # care instructions (≥3 madde): 7p, fitCode: 3p, fabricMaterialCode: 3p
    k = 0

    if product_data.get("fabricMaterialName"):
        k += 5
    else:
        eksik.append("Kumaş Adı")

    if product_data.get("fabricMaterialCode"):
        k += 3
    else:
        uyarilar.append("Kumaş Kodu girilmemiş")

    # mainMaterialContent: barcodes içinde herhangi birinde olmalı, % içermeli
    mat_contents = [
        b.get("mainMaterialContent", "")
        for b in barcodes
        if b.get("mainMaterialContent")
    ]
    if mat_contents:
        has_pct = any("%" in m for m in mat_contents)
        has_detail = any(len(m) > 20 for m in mat_contents)  # detaylı içerik
        if has_pct and has_detail:
            k += 7
        elif has_pct:
            k += 5
            hatali.append({"alan": "Kumaş İçerik %", "sorun": "% var ama detay az (ör. 'Pamuk %95, Elastan %5 | Astar: Pamuk %100')"})
        else:
            k += 2
            hatali.append({"alan": "Kumaş İçerik %", "sorun": "% içeriği bulunamadı — oranları ekleyin"})
    else:
        eksik.append("Kumaş İçerik % (ör: Pamuk %95, Elastan %5)")

    # Bakım talimatları
    care_count = len(care) if care else 0
    if care_count >= 3:
        k += 7
    elif care_count >= 1:
        k += 4
        hatali.append({"alan": "Bakım Talimatları", "sorun": f"sadece {care_count} bakım talimatı var, en az 3 olmalı"})
    else:
        eksik.append("Bakım Talimatları")

    if product_data.get("fitCode"):
        k += 3
    else:
        eksik.append("Kalıp Tipi (Slim/Regular/Oversize)")

    # ── Görsel (25 puan) ─────────────────────────────────────────────────────
    # Görsel sayısı: ≥5=12p, 3-4=9p, 2=6p, 1=3p, 0=0p
    # Renk çeşitliliği (her renk için görsel): 8p
    # colorHexCode tanımlı: 3p, görsel tip çeşitliliği: 2p
    g = 0
    img_count = len(images)

    if img_count >= 5:
        g += 12
    elif img_count >= 3:
        g += 9
    elif img_count == 2:
        g += 6
    elif img_count == 1:
        g += 3
        uyarilar.append("Sadece 1 görsel var — en az 3 görsel önerilir")
    else:
        eksik.append("Ürün Görseli")

    # Renk başına görsel
    renk_kodlari = set(b.get("colorCode", "") for b in barcodes if b.get("colorCode"))
    gorsel_renkleri = set(img.get("colorCode", "") for img in images if img.get("colorCode"))

    if renk_kodlari:
        eksik_renk = renk_kodlari - gorsel_renkleri
        if not eksik_renk:
            g += 8
        elif len(eksik_renk) <= len(renk_kodlari) // 2:
            g += 4
            uyarilar.append(f"{len(eksik_renk)}/{len(renk_kodlari)} renk için görsel eksik: {', '.join(list(eksik_renk)[:3])}")
        else:
            g += 1
            hatali.append({"alan": "Ürün Görseli (renk çeşitliliği)", "sorun": f"{len(eksik_renk)} rengin görseli yok"})
    elif img_count > 0:
        g += 4

    # colorHexCode
    if any(b.get("colorHexCode") for b in barcodes):
        g += 3
    else:
        uyarilar.append("Renk Hex Kodu girilmemiş")

    # Görsel tip çeşitliliği (manken/ürün/detay)
    img_types = set(img.get("type", "") for img in images if img.get("type"))
    if len(img_types) >= 2:
        g += 2
    elif img_count > 0:
        uyarilar.append("Görsel çeşidi az — manken, ürün ve detay fotoğrafı önerilir")

    # ── Satış İçerik (25 puan) ───────────────────────────────────────────────
    # description uzunluğu (≥80: 8p, ≥50: 6p, ≥20: 3p): 8p
    # ecomTag1-4 (her biri 2p): 8p
    # productThemeCode: 4p, notes (≥30 kar): 3p, ilişkili ürün/hikaye: 2p
    s = 0

    if len(desc) >= 80:
        s += 8
    elif len(desc) >= 50:
        s += 6
        uyarilar.append(f"Açıklama {len(desc)} karakter — 80+ önerilir")
    elif len(desc) >= 20:
        s += 3
        hatali.append({"alan": "Ürün Açıklaması (kısa)", "sorun": f"kısa ({len(desc)} karakter, ideal ≥80 — e-ticaret arama için)"})
    elif desc:
        s += 1
        hatali.append({"alan": "Ürün Açıklaması (kısa)", "sorun": f"çok kısa ({len(desc)} karakter)"})

    # ecomTags
    for tag_n in range(1, 5):
        tag_key = f"ecomTag{tag_n}Code"
        if product_data.get(tag_key):
            s += 2
        elif tag_n == 1:
            eksik.append(f"1. E-Ticaret Etiketi (Zorunlu)")
        else:
            uyarilar.append(f"ecomTag{tag_n}Code atanmamış")

    if product_data.get("productThemeCode"):
        s += 4
    else:
        eksik.append("Koleksiyon Teması")

    if len(notes) >= 30:
        s += 3
    elif notes:
        s += 1
        uyarilar.append(f"Notlar kısa ({len(notes)} kar.) — daha açıklayıcı olabilir")
    else:
        eksik.append("Ürün Notu / Stil Önerisi")

    # İlişkili ürün veya hikaye
    if relations or stories:
        s += 2
    else:
        uyarilar.append("İlişkili ürün veya koleksiyon hikayesi eklenmemiş")

    # ── Toplam ───────────────────────────────────────────────────────────────
    total = min(t, 25) + min(k, 25) + min(g, 25) + min(s, 25)
    grade = calc_grade(total)

    return {
        "urun_kodu":          stock_code,
        "sezon_kodu":         product_data.get("seasonCode"),
        "sezon_adi":          product_data.get("seasonName"),
        "quality_score":      total,
        "quality_grade":      grade,
        "score_temel_bilgi":  min(t, 25),
        "score_kumas_bilgi":  min(k, 25),
        "score_gorsel":       min(g, 25),
        "score_satis_icerik": min(s, 25),
        "eksik_alanlar":      eksik,
        "hatali_alanlar":     hatali,
        "uyarilar":           uyarilar,
    }


def _merge_mcp_into_product_data(product_data: dict, mcp_details: dict) -> dict:
    """
    Pimland MCP'den gelen get_products_with_squ verisini DB tabanlı
    product_data ile birleştir. MCP verisi önceliklidir.
    """
    if not mcp_details:
        return product_data

    merged = dict(product_data)

    # MCP'den gelen zengin alanlar
    for field in [
        "description", "notes", "vatRate",
        "fitCode", "fitName", "productTypeCode", "productTypeName",
        "fabricMaterialCode", "fabricMaterialName",
        "ecomTag1Code", "ecomTag1Name",
        "ecomTag2Code", "ecomTag2Name",
        "ecomTag3Code", "ecomTag3Name",
        "ecomTag4Code", "ecomTag4Name",
        "productThemeCode", "productThemeName",
        "productGroupCode", "productGroupName",
        "productMainGroupCode", "productMainGroupName",
        "brandCode", "brandName",
        "seasonCode", "seasonName",
        "isBlocked", "useInternet", "hasDiscount", "isNewProduct",
    ]:
        val = mcp_details.get(field)
        if val is not None:
            merged[field] = val

    # Barcodes — mainMaterialContent ve colorHexCode zengin gelir
    if mcp_details.get("barcodes"):
        merged["barcodes"] = mcp_details["barcodes"]

    # Görseller — tüm renk ve tipler
    if mcp_details.get("productImages"):
        merged["productImages"] = mcp_details["productImages"]

    # Bakım talimatları
    care = mcp_details.get("washingAndCareInstructions") or []
    if care:
        merged["washingAndCareInstructions"] = care

    # İlişkili ürünler
    relations = mcp_details.get("productRelations") or []
    if relations:
        merged["productRelations"] = relations

    # Hikayeler
    stories = mcp_details.get("productStories") or []
    if stories:
        merged["productStories"] = stories

    return merged


async def score_season(
    season_code: str,
    db_conn: "psycopg2.connection",
    progress_callback: Optional[Callable] = None,
    use_mcp: bool = True,
) -> dict:
    """
    Sezonun tüm ürünlerini puanlar.
    use_mcp=True (varsayılan): Pimland MCP'den gerçek veri çeker,
    DB verisiyle birleştirir. Daha doğru ama yavaş.
    use_mcp=False: Sadece DB verisi (hızlı ama eksik).
    """
    import json
    started = time.perf_counter()

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

    log.info("quality_scorer.season_start", season=season_code, total=total, use_mcp=use_mcp)

    # MCP'den sezon bazlı toplu veri çek
    # get_products_by_filter(season=X) tüm ürünleri sayfalı döndürür
    mcp_map: Dict[str, Any] = {}
    if use_mcp:
        try:
            from app.connectors.pimland_live import fetch_season_products
            if progress_callback:
                progress_callback(0, total)
            mcp_map = await fetch_season_products(season_code)
            log.info("quality_scorer.mcp_done",
                     mcp_fetched=len(mcp_map), total=total,
                     coverage_pct=round(len(mcp_map) / total * 100, 1))
            if progress_callback:
                progress_callback(len(mcp_map), total)
        except Exception as e:
            log.warning("quality_scorer.mcp_failed", error=str(e))

    results = []
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    sorun_sayac: Dict[str, int] = {}

    for i, row in enumerate(db_rows):
        color_list = [
            {"colorCode": c.strip(), "mainMaterialContent": "", "colorHexCode": None}
            for c in (row.get("color_codes") or "").split(",")
            if c.strip()
        ]

        # Temel DB verisi
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
            "fabricMaterialCode":  None,
            "fitCode":             None,
            "ecomTag1Code":        None,
            "ecomTag2Code":        None,
            "ecomTag3Code":        None,
            "ecomTag4Code":        None,
            "notes":               None,
            "productTypeCode":     None,
            "vatRate":             10,
            "productImages":       [{"colorCode": row["first_color_code"], "type": "product"}]
                                   if row.get("default_image_url") else [],
            "barcodes":            color_list,
            "washingAndCareInstructions": [],
            "productRelations":    [],
            "productStories":      [],
        }

        # MCP verisi varsa DB verisinin üzerine yaz
        mcp_details = mcp_map.get(row["urun_kodu"])
        if mcp_details:
            product_data = _merge_mcp_into_product_data(product_data, mcp_details)

        result = score_product(row["urun_kodu"], product_data)
        results.append(result)
        grade_counts[result["quality_grade"]] = grade_counts.get(result["quality_grade"], 0) + 1
        for eksik in result["eksik_alanlar"]:
            sorun_sayac[eksik] = sorun_sayac.get(eksik, 0) + 1

        if progress_callback and (i + 1) % 10 == 0:
            progress_callback(i + 1, total)

    # Toplu UPSERT
    with db_conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO enrichment_quality (
                urun_kodu, sezon_kodu, sezon_adi,
                quality_score, quality_grade,
                score_temel_bilgi, score_kumas_bilgi,
                score_gorsel, score_satis_icerik,
                eksik_alanlar, hatali_alanlar, uyarilar
            ) VALUES %s
            ON CONFLICT (urun_kodu) DO UPDATE SET
                sezon_kodu=EXCLUDED.sezon_kodu, sezon_adi=EXCLUDED.sezon_adi,
                quality_score=EXCLUDED.quality_score, quality_grade=EXCLUDED.quality_grade,
                score_temel_bilgi=EXCLUDED.score_temel_bilgi,
                score_kumas_bilgi=EXCLUDED.score_kumas_bilgi,
                score_gorsel=EXCLUDED.score_gorsel,
                score_satis_icerik=EXCLUDED.score_satis_icerik,
                eksik_alanlar=EXCLUDED.eksik_alanlar,
                hatali_alanlar=EXCLUDED.hatali_alanlar,
                uyarilar=EXCLUDED.uyarilar,
                last_scored_at=NOW()
            """,
            [(
                r["urun_kodu"], r["sezon_kodu"], r["sezon_adi"],
                r["quality_score"], r["quality_grade"],
                r["score_temel_bilgi"], r["score_kumas_bilgi"],
                r["score_gorsel"], r["score_satis_icerik"],
                json.dumps(r["eksik_alanlar"], ensure_ascii=False),
                json.dumps(r["hatali_alanlar"], ensure_ascii=False),
                json.dumps(r["uyarilar"], ensure_ascii=False),
            ) for r in results],
            page_size=200,
        )

        ort_puan = round(sum(r["quality_score"] for r in results) / total, 1)
        top_sorunlar = sorted(
            [{"sorun": k, "sayi": v, "pct": round(v / total * 100)} for k, v in sorun_sayac.items()],
            key=lambda x: -x["sayi"]
        )[:10]

        season_name = db_rows[0]["sezon_adi"] if db_rows else season_code
        cur.execute("""
            INSERT INTO enrichment_season_summary
                (sezon_kodu, sezon_adi, toplam_urun, ortalama_puan,
                 grade_a, grade_b, grade_c, grade_d, grade_f, top_sorunlar, scored_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (sezon_kodu) DO UPDATE SET
                sezon_adi=EXCLUDED.sezon_adi, toplam_urun=EXCLUDED.toplam_urun,
                ortalama_puan=EXCLUDED.ortalama_puan,
                grade_a=EXCLUDED.grade_a, grade_b=EXCLUDED.grade_b,
                grade_c=EXCLUDED.grade_c, grade_d=EXCLUDED.grade_d,
                grade_f=EXCLUDED.grade_f,
                top_sorunlar=EXCLUDED.top_sorunlar, scored_at=NOW()
        """, (
            season_code, season_name, total, ort_puan,
            grade_counts["A"], grade_counts["B"], grade_counts["C"],
            grade_counts["D"], grade_counts["F"],
            json.dumps(top_sorunlar, ensure_ascii=False),
        ))

    db_conn.commit()
    sure = round(time.perf_counter() - started, 1)
    log.info("quality_scorer.season_done", season=season_code, total=total, avg=ort_puan, sure_sn=sure)

    if progress_callback:
        progress_callback(total, total)

    return {
        "season_code": season_code, "toplam_urun": total,
        "ortalama_puan": ort_puan, "grade_counts": grade_counts, "sure_sn": sure,
    }
