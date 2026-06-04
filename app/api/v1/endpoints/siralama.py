"""E-Ticaret Kategori Sıralama Agent — Dinamik ürün sıralama motoru."""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session, get_session
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/siralama", tags=["siralama"])

# ── Sabit ağırlıklar ─────────────────────────────────────────────────────────
DEFAULT_WEIGHTS = {
    "momentum": 0.20,
    "gorsel":   0.18,
    "donusum":  0.15,
    "stok":     0.15,
    "iade":     0.12,
    "yenilik":  0.12,
    "marj":     0.08,
}

IMG_BASE = "https://img-adl.sm.mncdn.com/cdnimages/products"

# ── GET /filters ──────────────────────────────────────────────────────────────

@router.get("/filters")
async def get_filters(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> Dict[str, Any]:
    """Sezon, marka, kategori seçenekleri."""
    sezon_rows = (await session.execute(text("""
        SELECT DISTINCT sezon_kodu, sezon_adi
        FROM pim_products WHERE internet_aktif=true AND sezon_kodu IS NOT NULL
        ORDER BY sezon_kodu DESC LIMIT 30
    """))).fetchall()

    marka_rows = (await session.execute(text("""
        SELECT DISTINCT marka_adi FROM pim_products
        WHERE internet_aktif=true AND marka_adi IS NOT NULL ORDER BY marka_adi
    """))).fetchall()

    kat_rows = (await session.execute(text("""
        SELECT DISTINCT ana_grup_adi FROM pim_products
        WHERE internet_aktif=true AND ana_grup_adi IS NOT NULL ORDER BY ana_grup_adi
    """))).fetchall()

    return {
        "sezonlar": [{"kod": r[0], "adi": r[1]} for r in sezon_rows],
        "markalar": [r[0] for r in marka_rows],
        "kategoriler": [r[0] for r in kat_rows],
    }


# ── POST /hesapla ─────────────────────────────────────────────────────────────

@router.post("/hesapla")
async def hesapla(
    body: Dict[str, Any],
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> Dict[str, Any]:
    """Seçilen kategori için ürünleri puan-sıralama motorundan geçirir."""
    sezon_kodu: str = body.get("sezon_kodu", "")
    marka_adi:  str = body.get("marka_adi", "")
    kategori:   str = body.get("kategori", "")
    agirliklar: Dict[str, float] = body.get("agirliklar") or DEFAULT_WEIGHTS

    if not all([sezon_kodu, marka_adi, kategori]):
        return {"error": "sezon_kodu, marka_adi, kategori zorunlu"}

    # 1. MCP'den sezon ürünlerini çek (cache hit olabilir)
    mcp_map: Dict[str, Any] = {}
    try:
        from app.connectors.pimland_live import fetch_season_products
        mcp_map = await asyncio.wait_for(
            fetch_season_products(sezon_kodu), timeout=120
        )
    except Exception as e:
        log.warning("siralama.mcp_failed", error=str(e))

    # Kategori + marka filtresi
    filtered = []
    for kod, p in mcp_map.items():
        brand = (p.get("brandName") or "").strip()
        group = (p.get("productMainGroupName") or "").strip()
        if brand == marka_adi and group == kategori:
            filtered.append((kod, p))

    # Fallback: DB'den al (MCP başarısız olursa)
    if not filtered:
        db_rows = (await session.execute(text("""
            SELECT urun_kodu, urun_adi, marka_adi, fabricmaterialname,
                   color_codes, first_color_code, default_image_url
            FROM pim_products
            WHERE sezon_kodu=:s AND marka_adi=:m AND ana_grup_adi=:k
              AND internet_aktif=true
        """), {"s": sezon_kodu, "m": marka_adi, "k": kategori})).mappings().all()

        for row in db_rows:
            kod = row["urun_kodu"]
            filtered.append((kod, {
                "stockCode": kod,
                "description": row["urun_adi"] or "",
                "brandName": row["marka_adi"] or "",
                "productMainGroupName": kategori,
                "fabricMaterialName": row["fabricmaterialname"] or "",
                "productImages": [{"name": f"{kod}_{row['first_color_code']}_1.jpg"}]
                                  if row.get("first_color_code") else [],
                "barcodes": [{"colorCode": c.strip()} for c in
                             (row.get("color_codes") or "").split(",") if c.strip()],
                "isNewProduct": False,
                "washingAndCareInstructions": [],
                "notes": "",
                "ecomTag1Code": None,
            }))

    if not filtered:
        return {"job_id": "", "urunler": [], "toplam_urun": 0,
                "uyari": "Bu kombinasyon için ürün bulunamadı."}

    stock_codes = [k for k, _ in filtered]

    # 2. Satış verisi (yerel DB, son ~30 ve ~7 gün)
    now = datetime.now()
    y30, m30 = (now - timedelta(days=30)).year, (now - timedelta(days=30)).month
    y7,  m7  = (now - timedelta(days=7)).year,  (now - timedelta(days=7)).month

    sales_rows = (await session.execute(text("""
        SELECT urun_kodu,
               SUM(adet) FILTER (WHERE yil*100+ay >= :ym30) AS adet_30g,
               SUM(adet) FILTER (WHERE yil*100+ay >= :ym7)  AS adet_7g,
               SUM(tutar) FILTER (WHERE yil*100+ay >= :ym30) AS ciro_30g
        FROM incorta_satis
        WHERE urun_kodu = ANY(:codes)
          AND yil*100+ay >= :ym30
        GROUP BY urun_kodu
    """), {
        "codes": stock_codes,
        "ym30": y30 * 100 + m30,
        "ym7":  y7  * 100 + m7,
    })).mappings().all()
    sales_map = {r["urun_kodu"]: dict(r) for r in sales_rows}

    # 3. İade verisi (son ~90 gün)
    y90, m90 = (now - timedelta(days=90)).year, (now - timedelta(days=90)).month
    iade_rows = (await session.execute(text("""
        SELECT urun_kodu,
               ABS(SUM(adet)) AS iade_adet_90g,
               ABS(SUM(tutar)) AS iade_ciro_90g
        FROM incorta_depo_iade
        WHERE urun_kodu = ANY(:codes)
          AND yil*100+ay >= :ym90
        GROUP BY urun_kodu
    """), {"codes": stock_codes, "ym90": y90 * 100 + m90})).mappings().all()
    iade_map = {r["urun_kodu"]: dict(r) for r in iade_rows}

    # 4. Skorla
    scored = _score_products(filtered, sales_map, iade_map, agirliklar)

    # 5. Kuralları uygula
    ranked, kural_aciklamalari = _apply_rules(scored)

    # 6. Sonuç yapısını oluştur
    job_id = str(uuid.uuid4())[:8]
    urunler = []
    for i, p in enumerate(ranked):
        kod = p["stock_code"]
        mcp = p["_mcp"]
        images = mcp.get("productImages") or []
        img_name = images[0].get("name", "") if images else ""
        img_url = f"{IMG_BASE}/{img_name}" if img_name else None

        urunler.append({
            "sira":          i + 1,
            "stock_code":    kod,
            "urun_adi":      mcp.get("description") or kod,
            "image_url":     img_url,
            "toplam_puan":   round(p["toplam_puan"], 1),
            "kriter_puanlari": {k: round(v, 1) for k, v in p["kriter_puanlari"].items()},
            "uyari_flags":   p.get("uyari_flags", []),
            "is_new":        bool(mcp.get("isNewProduct")),
            "gorsel_sayisi": p.get("gorsel_sayisi", 0),
            "aktif_beden":   p.get("aktif_beden", 0),
            "adet_7g":       int(sales_map.get(kod, {}).get("adet_7g") or 0),
            "adet_30g":      int(sales_map.get(kod, {}).get("adet_30g") or 0),
            "iade_adet_90g": int(iade_map.get(kod, {}).get("iade_adet_90g") or 0),
            "ecomTag1":      mcp.get("ecomTag1Name"),
            "marka":         mcp.get("brandName", marka_adi),
        })

    return {
        "job_id":             job_id,
        "sezon_kodu":         sezon_kodu,
        "marka_adi":          marka_adi,
        "kategori":           kategori,
        "toplam_urun":        len(urunler),
        "agirliklar":         agirliklar,
        "urunler":            urunler,
        "kural_uygulamalari": kural_aciklamalari,
        "veri_tarihi":        now.isoformat(),
        "_raw_for_save":      {  # approve endpoint bunu saklar
            "siralama_json": [{"sira": u["sira"], "stock_code": u["stock_code"],
                               "toplam_puan": u["toplam_puan"],
                               "kriter_puanlari": u["kriter_puanlari"]}
                              for u in urunler],
            "ozet_json": {"agirliklar": agirliklar,
                         "kural_uygulamalari": kural_aciklamalari},
        },
    }


# ── POST /onay/{job_id} ───────────────────────────────────────────────────────

@router.post("/onay")
async def onayla(
    body: Dict[str, Any],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Dict[str, Any]:
    """Sıralamayı DB'e kaydet."""
    job_id     = body.get("job_id", "")
    sezon      = body.get("sezon_kodu", "")
    marka      = body.get("marka_adi", "")
    kategori   = body.get("kategori", "")
    toplam     = body.get("toplam_urun", 0)
    raw        = body.get("_raw_for_save", {})

    if not job_id:
        return {"error": "job_id zorunlu"}

    await session.execute(text("""
        INSERT INTO siralama_gecmisi
            (job_id, sezon_kodu, marka_adi, kategori, toplam_urun,
             onayli, onay_tarihi, siralama_json, ozet_json)
        VALUES (:job_id, :sezon, :marka, :kategori, :toplam,
                true, NOW(), :sir_json, :ozet_json)
        ON CONFLICT (job_id) DO UPDATE SET
            onayli=true, onay_tarihi=NOW()
    """), {
        "job_id":    job_id,
        "sezon":     sezon,
        "marka":     marka,
        "kategori":  kategori,
        "toplam":    toplam,
        "sir_json":  __import__("json").dumps(raw.get("siralama_json", []), ensure_ascii=False),
        "ozet_json": __import__("json").dumps(raw.get("ozet_json", {}), ensure_ascii=False),
    })
    await session.commit()
    return {"saved": True, "job_id": job_id}


# ── GET /gecmis ───────────────────────────────────────────────────────────────

@router.get("/gecmis")
async def get_gecmis(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    sezon:    Optional[str] = Query(None),
    marka:    Optional[str] = Query(None),
    kategori: Optional[str] = Query(None),
    page:     int = Query(1, ge=1),
    limit:    int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    """Kayıtlı sıralama geçmişi."""
    conds = []
    params: Dict[str, Any] = {"offset": (page - 1) * limit, "limit": limit}
    if sezon:    conds.append("sezon_kodu=:sezon");    params["sezon"]    = sezon
    if marka:    conds.append("marka_adi=:marka");     params["marka"]    = marka
    if kategori: conds.append("kategori=:kategori");   params["kategori"] = kategori
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    total = (await session.execute(text(
        f"SELECT COUNT(*) FROM siralama_gecmisi {where}"
    ), params)).scalar() or 0

    rows = (await session.execute(text(f"""
        SELECT id, job_id, sezon_kodu, marka_adi, kategori,
               toplam_urun, onayli, onay_tarihi, created_at
        FROM siralama_gecmisi {where}
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)).mappings().all()

    items = []
    for r in rows:
        d = dict(r)
        d["created_at"]  = d["created_at"].isoformat() if d.get("created_at") else None
        d["onay_tarihi"] = d["onay_tarihi"].isoformat() if d.get("onay_tarihi") else None
        items.append(d)

    return {"total": total, "page": page, "limit": limit, "items": items}


# ═══════════════════════════════════════════════════════════════════════════════
# SIRALAMA ALGORİTMASI
# ═══════════════════════════════════════════════════════════════════════════════

def _norm(values: List[float]) -> List[float]:
    """Min-max normalisation. Tüm eşitse 50 döner."""
    mn, mx = min(values), max(values)
    if mx == mn:
        return [50.0] * len(values)
    return [(v - mn) / (mx - mn) * 100 for v in values]


def _score_products(
    filtered: List[tuple],
    sales_map: Dict,
    iade_map: Dict,
    weights: Dict[str, float],
) -> List[Dict]:
    """Her ürün için 7 kriter skoru hesapla, ağırlıklı toplamı bul."""
    now = datetime.now()
    items = []

    for kod, mcp in filtered:
        s = sales_map.get(kod, {})
        ia = iade_map.get(kod, {})

        adet_30 = float(s.get("adet_30g") or 0)
        adet_7  = float(s.get("adet_7g")  or 0)
        iade_90 = float(ia.get("iade_adet_90g") or 0)

        images   = mcp.get("productImages") or []
        barcodes = mcp.get("barcodes") or []
        desc     = (mcp.get("notes") or mcp.get("description") or "").strip()
        care     = mcp.get("washingAndCareInstructions") or []

        gorsel_sayisi = len(images)
        aktif_beden   = len([b for b in barcodes if not b.get("isBlocked")])
        is_new        = bool(mcp.get("isNewProduct"))

        # Momentum raw
        momentum_raw = math.log1p(adet_7) * 0.6 + math.log1p(adet_30) * 0.4
        trend = (adet_7 / max(adet_30 / 4, 0.1)) if adet_30 > 0 else 1.0
        hizlaniyor = trend > 1.2
        yavashiyor = trend < 0.8

        # Görsel skoru
        gp = {0: 0, 1: 10, 2: 35, 3: 55, 4: 70, 5: 85}.get(min(gorsel_sayisi, 5), 100)
        if gorsel_sayisi >= 6: gp = 100
        desc_len = len(desc)
        if desc_len >= 300:   ap = 100
        elif desc_len >= 150: ap = 70
        elif desc_len >= 50:  ap = 40
        else:                 ap = 10
        gorsel_s = min(100, gp * 0.50 + min(len(care) / 3, 1) * 20 + ap * 0.25)

        # Stok skoru
        if aktif_beden == 0:
            stok_s = 0
        else:
            colors = len({b.get("colorCode", "") for b in barcodes if not b.get("isBlocked")})
            sizes  = max(aktif_beden / max(colors, 1), 1)
            doluluk = min(sizes / 5, 1) * 100  # 5 beden tam kabul
            stok_s = min(doluluk, 100)

        # İade skoru
        iade_oran = iade_90 / max(adet_30 * 3, 1)
        iade_s = max(0, 100 - iade_oran * 100)

        # Yenilik skoru
        if is_new:
            yenilik_s = 90.0
        else:
            # pim_products'tan tarih yoksa marka+sezon bazında nötr
            yenilik_s = 30.0

        # Dönüşüm (proxy)
        donusum_s = min(adet_30 / max(len(barcodes), 1) * 10, 100) if barcodes else 40.0

        # Marj/fiyat
        marj_s = 40.0 if mcp.get("hasDiscount") else 65.0

        items.append({
            "stock_code":    kod,
            "_mcp":          mcp,
            "gorsel_sayisi": gorsel_sayisi,
            "aktif_beden":   aktif_beden,
            "is_new":        is_new,
            "hizlaniyor":    hizlaniyor,
            "yavashiyor":    yavashiyor,
            "stok_sifir":    aktif_beden == 0,
            "iade_oran":     iade_oran,
            "_momentum_raw": momentum_raw,
            "_kriter_raw": {
                "gorsel":   gorsel_s,
                "stok":     stok_s,
                "iade":     iade_s,
                "yenilik":  yenilik_s,
                "donusum":  donusum_s,
                "marj":     marj_s,
            },
        })

    # Momentum normalize et
    mom_vals = [it["_momentum_raw"] for it in items]
    mom_norm = _norm(mom_vals)

    for it, mn in zip(items, mom_norm):
        m = mn
        if it["is_new"]:        m = 55.0
        elif it["hizlaniyor"]:  m = min(100, m + 12)
        elif it["yavashiyor"]:  m = max(0, m - 12)
        it["_kriter_raw"]["momentum"] = m

    # Ağırlıklı toplam
    w = {**DEFAULT_WEIGHTS, **weights}
    total_w = sum(w.values()) or 1
    w = {k: v / total_w for k, v in w.items()}  # normalize to 1

    for it in items:
        kr = it["_kriter_raw"]
        toplam = (
            kr["momentum"] * w.get("momentum", 0.20) +
            kr["gorsel"]   * w.get("gorsel",   0.18) +
            kr["donusum"]  * w.get("donusum",  0.15) +
            kr["stok"]     * w.get("stok",     0.15) +
            kr["iade"]     * w.get("iade",     0.12) +
            kr["yenilik"]  * w.get("yenilik",  0.12) +
            kr["marj"]     * w.get("marj",     0.08)
        )
        it["toplam_puan"]    = toplam
        it["kriter_puanlari"] = kr
        it["uyari_flags"]    = []
        if it["stok_sifir"]:            it["uyari_flags"].append("sifir_stok")
        if it["iade_oran"] > 0.35:      it["uyari_flags"].append("yuksek_iade")
        if it["gorsel_sayisi"] < 3:     it["uyari_flags"].append("az_gorsel")
        if it["is_new"]:                it["uyari_flags"].append("yeni_urun")
        if it["hizlaniyor"]:            it["uyari_flags"].append("hizlaniyor")

    return items


def _apply_rules(scored: List[Dict]) -> tuple:
    """Skor sonrası pozisyon kuralları."""
    aciklamalar = []

    # Sıfır stok sona
    stoklu  = [p for p in scored if not p["stok_sifir"]]
    stoksuz = [p for p in scored if p["stok_sifir"]]
    if stoksuz:
        aciklamalar.append(f"{len(stoksuz)} ürün stok=0 nedeniyle sona taşındı")

    # Yüksek iade ilk 8'e girmesin
    def _no_high_iade_top8(lst):
        top8 = lst[:8]
        rest = lst[8:]
        problem = [p for p in top8 if "yuksek_iade" in p["uyari_flags"]]
        safe    = [p for p in top8 if "yuksek_iade" not in p["uyari_flags"]]
        if problem:
            aciklamalar.append(f"{len(problem)} ürün yüksek iade nedeniyle ilk 8'den çıkarıldı")
        return safe + problem + rest
    stoklu = _no_high_iade_top8(sorted(stoklu, key=lambda x: x["toplam_puan"], reverse=True))

    # Anti-monotony: her 8 üründe aynı ana gruptan max 3
    def _anti_monotony(lst):
        count = {}
        result = []
        deferred = []
        for p in lst:
            group = (p["_mcp"].get("productGroupName") or "")
            cnt = count.get(group, 0)
            if cnt < 3:
                result.append(p)
                count[group] = cnt + 1
            else:
                deferred.append(p)
        n_moved = len(deferred)
        if n_moved:
            aciklamalar.append(f"{n_moved} ürün anti-monotony kuralıyla yeniden konumlandırıldı")
        return result + deferred
    stoklu = _anti_monotony(stoklu)

    # Yeni ürün ilk 20'de olsun
    yeni_idx = [i for i, p in enumerate(stoklu) if "yeni_urun" in p["uyari_flags"]]
    top20_limit = max(20, len(stoklu) // 5)
    for idx in yeni_idx:
        if idx >= top20_limit:
            p = stoklu.pop(idx)
            insert_at = min(top20_limit - 1, len(stoklu))
            stoklu.insert(insert_at, p)
            aciklamalar.append(f"Yeni ürün ({p['stock_code']}) ilk {top20_limit} içine alındı")

    return stoklu + stoksuz, aciklamalar
