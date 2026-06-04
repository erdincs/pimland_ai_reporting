"""Product Enrichment — Kalite Puanlama API."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Dict, List, Optional

import psycopg2
import redis.asyncio as aioredis
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session, get_session
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/enrichment", tags=["enrichment"])

_redis: aioredis.Redis = aioredis.from_url(
    settings.redis_url, encoding="utf-8", decode_responses=True
)

GRADE_ORDER = ["A", "B", "C", "D", "F"]


# ── GET /seasons ──────────────────────────────────────────────────────────────

@router.get("/seasons")
async def get_seasons(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    marka: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Pimland DB'deki sezonlar + enrichment durumu (sadece internet_aktif)."""
    params: Dict[str, Any] = {}
    marka_filter = ""
    if marka:
        marka_filter = "AND p.marka_adi = :marka"
        params["marka"] = marka

    rows = (await session.execute(text(f"""
        SELECT
            p.sezon_kodu,
            p.sezon_adi,
            COUNT(DISTINCT p.urun_kodu) AS toplam_urun,
            s.ortalama_puan,
            s.grade_a, s.grade_b, s.grade_c, s.grade_d, s.grade_f,
            s.scored_at,
            CASE WHEN s.sezon_kodu IS NOT NULL THEN true ELSE false END AS scored
        FROM pim_products p
        LEFT JOIN enrichment_season_summary s ON s.sezon_kodu = p.sezon_kodu
        WHERE p.sezon_kodu IS NOT NULL
          AND p.internet_aktif = true
          {marka_filter}
        GROUP BY p.sezon_kodu, p.sezon_adi,
                 s.sezon_kodu, s.ortalama_puan,
                 s.grade_a, s.grade_b, s.grade_c, s.grade_d, s.grade_f,
                 s.scored_at
        ORDER BY p.sezon_kodu DESC
    """), params)).mappings().all()

    return [dict(r) for r in rows]


# ── POST /score/{season_code} ─────────────────────────────────────────────────

def _run_scoring_bg(job_id: str, season_code: str, use_mcp: bool = True) -> None:
    """Background task — senkron psycopg2 ile çalışır."""
    import asyncio
    import redis.asyncio as _aioredis

    async def _update(redis_cli, key: str, data: dict) -> None:
        await redis_cli.setex(key, 7200, json.dumps(data, ensure_ascii=False))

    async def _run() -> None:
        # Yeni event loop'a ait Redis bağlantısı oluştur
        redis_cli = _aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        key = f"enrichment:job:{job_id}"
        await _update(redis_cli, key, {"status": "running", "current": 0, "total": 0,
                                        "use_mcp": use_mcp})

        conn = psycopg2.connect(
            host=settings.postgres_host, port=settings.postgres_port,
            dbname=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password,
        )
        try:
            from app.services.enrichment.quality_scorer import score_season

            def progress(current: int, total: int) -> None:
                asyncio.ensure_future(
                    _update(redis_cli, key, {"status": "running", "current": current,
                                              "total": total, "use_mcp": use_mcp})
                )

            result = await score_season(
                season_code, conn,
                progress_callback=progress,
                use_mcp=use_mcp,
            )
            await _update(redis_cli, key, {"status": "done", "use_mcp": use_mcp, **result})
        except Exception as e:
            log.error("enrichment.scoring_failed", season=season_code, error=str(e))
            await _update(redis_cli, key, {"status": "failed", "error": str(e)})
        finally:
            conn.close()
            await redis_cli.aclose()

    asyncio.run(_run())


@router.post("/score/{season_code}")
async def start_scoring(
    season_code: str,
    background_tasks: BackgroundTasks,
    use_mcp: bool = True,
) -> Dict[str, Any]:
    """Sezon puanlamasını arka planda başlat.
    use_mcp=true (varsayılan): Pimland MCP'den gerçek veri çeker (~3-5 dk).
    use_mcp=false: Sadece DB verisi, hızlı (~5 sn) ama eksik."""
    job_id = str(uuid.uuid4())[:8]
    key    = f"enrichment:job:{job_id}"
    await _redis.setex(key, 3600, json.dumps({"status": "queued", "current": 0, "total": 0}))

    background_tasks.add_task(_run_scoring_bg, job_id, season_code, use_mcp)
    return {
        "job_id": job_id,
        "status": "started",
        "season_code": season_code,
        "use_mcp": use_mcp,
        "info": ("MCP ile tam puanlama (~3-5 dk). MCP kapalıysa ?use_mcp=false deneyin."
                 if use_mcp else "DB tabanlı hızlı puanlama (~5 sn)."),
    }


# ── GET /score/status/{job_id} ────────────────────────────────────────────────

@router.get("/score/status/{job_id}")
async def get_job_status(job_id: str) -> Dict[str, Any]:
    raw = await _redis.get(f"enrichment:job:{job_id}")
    if not raw:
        return {"status": "not_found"}
    return json.loads(raw)


# ── GET /dashboard/{season_code} ─────────────────────────────────────────────

@router.get("/dashboard/{season_code}")
async def get_dashboard(
    season_code: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    marka: Optional[str] = None,
) -> Dict[str, Any]:
    """Sezon dashboard — sadece internet_aktif ürünler."""
    params: Dict[str, Any] = {"sc": season_code}
    marka_filter = ""
    if marka:
        marka_filter = "AND p.marka_adi = :marka"
        params["marka"] = marka

    # Internet aktif ürünlerin özeti
    stats = (await session.execute(text(f"""
        SELECT
            COUNT(*) AS toplam_urun,
            ROUND(AVG(eq.quality_score)::numeric, 1) AS ortalama_puan,
            COUNT(*) FILTER (WHERE eq.quality_grade='A') AS grade_a,
            COUNT(*) FILTER (WHERE eq.quality_grade='B') AS grade_b,
            COUNT(*) FILTER (WHERE eq.quality_grade='C') AS grade_c,
            COUNT(*) FILTER (WHERE eq.quality_grade='D') AS grade_d,
            COUNT(*) FILTER (WHERE eq.quality_grade='F') AS grade_f,
            MAX(eq.last_scored_at) AS scored_at
        FROM enrichment_quality eq
        JOIN pim_products p ON p.urun_kodu = eq.urun_kodu
        WHERE eq.sezon_kodu = :sc
          AND p.internet_aktif = true
          {marka_filter}
    """), params)).mappings().first()

    if not stats or not stats["toplam_urun"]:
        return {"error": f"'{season_code}' için internet aktif puanlanmış ürün bulunamadı"}

    # Puan dağılımı
    dist_rows = (await session.execute(text(f"""
        SELECT (eq.quality_score / 10) * 10 AS aralik_bas, COUNT(*) AS sayi
        FROM enrichment_quality eq
        JOIN pim_products p ON p.urun_kodu = eq.urun_kodu
        WHERE eq.sezon_kodu = :sc AND p.internet_aktif = true {marka_filter}
        GROUP BY aralik_bas ORDER BY aralik_bas
    """), params)).mappings().all()

    puan_dagilimi = []
    for i in range(0, 100, 10):
        row = next((r for r in dist_rows if r["aralik_bas"] == i), None)
        puan_dagilimi.append({"aralik": f"{i}-{i+10}", "sayi": row["sayi"] if row else 0})

    # Top sorunlar — en sık eksik alanlar
    sorun_rows = (await session.execute(text(f"""
        SELECT eksik_alan, COUNT(*) AS sayi
        FROM enrichment_quality eq
        JOIN pim_products p ON p.urun_kodu = eq.urun_kodu
        CROSS JOIN LATERAL jsonb_array_elements_text(eq.eksik_alanlar) AS eksik_alan
        WHERE eq.sezon_kodu = :sc AND p.internet_aktif = true {marka_filter}
        GROUP BY eksik_alan ORDER BY sayi DESC LIMIT 10
    """), params)).mappings().all()

    total = stats["toplam_urun"] or 1
    top_sorunlar = [
        {"sorun": r["eksik_alan"], "sayi": r["sayi"], "pct": round(r["sayi"] / total * 100)}
        for r in sorun_rows
    ]

    sezon_adi = (await session.execute(
        text("SELECT sezon_adi FROM pim_products WHERE sezon_kodu=:sc LIMIT 1"),
        {"sc": season_code}
    )).scalar()

    return {
        "season_code":   season_code,
        "season_name":   sezon_adi or season_code,
        "toplam_urun":   stats["toplam_urun"],
        "ortalama_puan": float(stats["ortalama_puan"] or 0),
        "grade_dist": {
            "A": stats["grade_a"], "B": stats["grade_b"],
            "C": stats["grade_c"], "D": stats["grade_d"],
            "F": stats["grade_f"],
        },
        "puan_dagilimi": puan_dagilimi,
        "top_sorunlar":  top_sorunlar,
        "scored_at":     stats["scored_at"],
    }


# ── GET /products/{season_code} ───────────────────────────────────────────────

@router.get("/products/{season_code}")
async def get_products(
    season_code: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    grade: Optional[str] = None,
    sorun: Optional[str] = None,
    sort: str = "score_asc",
    page: int = 1,
    limit: int = 50,
    marka: Optional[str] = None,
) -> Dict[str, Any]:
    conditions = ["eq.sezon_kodu = :sc", "p.internet_aktif = true"]
    params: Dict[str, Any] = {"sc": season_code}

    if marka:
        conditions.append("p.marka_adi = :marka")
        params["marka"] = marka

    if grade:
        grades = [g.strip().upper() for g in grade.split(",")]
        conditions.append(f"eq.quality_grade = ANY(ARRAY{grades})")

    if sorun == "eksik_kumas":
        conditions.append("eq.eksik_alanlar @> '[\"Kumaş Adı\"]'")
    elif sorun == "eksik_gorsel":
        conditions.append("eq.eksik_alanlar @> '[\"Ürün Görseli\"]'")
    elif sorun == "kisa_aciklama":
        conditions.append("eq.hatali_alanlar::text ILIKE '%Açıklama%'")
    elif sorun == "eksik_tema":
        conditions.append("eq.eksik_alanlar @> '[\"Koleksiyon Teması\"]'")

    order = {
        "score_asc":  "eq.quality_score ASC",
        "score_desc": "eq.quality_score DESC",
        "urun_kodu":  "eq.urun_kodu ASC",
    }.get(sort, "eq.quality_score ASC")

    where = " AND ".join(conditions)
    offset = (page - 1) * limit
    params.update({"lim": limit, "off": offset})

    count_row = (await session.execute(
        text(f"SELECT COUNT(*) FROM enrichment_quality eq JOIN pim_products p ON p.urun_kodu=eq.urun_kodu WHERE {where}"), params
    )).scalar()

    rows = (await session.execute(text(f"""
        SELECT
            eq.urun_kodu, p.urun_adi,
            p.default_image_url AS gorsel_url,
            eq.quality_score, eq.quality_grade,
            eq.score_temel_bilgi, eq.score_kumas_bilgi,
            eq.score_gorsel, eq.score_satis_icerik,
            jsonb_array_length(eq.eksik_alanlar) AS eksik_sayisi,
            jsonb_array_length(eq.hatali_alanlar) AS hatali_sayisi
        FROM enrichment_quality eq
        LEFT JOIN pim_products p ON p.urun_kodu = eq.urun_kodu
        WHERE {where}
        ORDER BY {order}
        LIMIT :lim OFFSET :off
    """), params)).mappings().all()

    return {
        "total": count_row or 0,
        "page":  page,
        "items": [dict(r) for r in rows],
    }


# ── GET /product/{urun_kodu} ──────────────────────────────────────────────────

@router.get("/product/{urun_kodu}")
async def get_product_detail(
    urun_kodu: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> Dict[str, Any]:
    row = (await session.execute(text("""
        SELECT
            eq.urun_kodu, eq.sezon_kodu, eq.sezon_adi,
            eq.quality_score, eq.quality_grade,
            eq.score_temel_bilgi, eq.score_kumas_bilgi,
            eq.score_gorsel, eq.score_satis_icerik,
            eq.eksik_alanlar, eq.hatali_alanlar, eq.uyarilar,
            eq.last_scored_at, eq.detail_json,
            p.urun_adi, p.default_image_url,
            p.marka_adi, p.sezon_adi AS p_sezon_adi, p.sezon_kodu AS p_sezon_kodu,
            p.ana_grup_adi, p.urun_grubu_adi, p.tema_adi,
            p.fabricmaterialname, p.color_codes, p.internet_aktif, p.bloke
        FROM enrichment_quality eq
        LEFT JOIN pim_products p ON p.urun_kodu = eq.urun_kodu
        WHERE eq.urun_kodu = :uk
    """), {"uk": urun_kodu})).mappings().first()

    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, f"'{urun_kodu}' henüz puanlanmamış")

    d = dict(row)

    # Dolu alanların değerlerini ayrı bir dict olarak sun
    dolu = {}
    if d.get("marka_adi"):          dolu["Marka"]           = d["marka_adi"]
    if d.get("sezon_adi"):          dolu["Sezon"]           = f"{d['sezon_adi']} ({d.get('sezon_kodu','')})"
    if d.get("ana_grup_adi"):       dolu["Ana Kategori"]    = d["ana_grup_adi"]
    if d.get("urun_grubu_adi"):     dolu["Ürün Grubu"]      = d["urun_grubu_adi"]
    if d.get("tema_adi"):           dolu["Tema"]            = d["tema_adi"]
    if d.get("fabricmaterialname"): dolu["Kumaş"]           = d["fabricmaterialname"]
    if d.get("color_codes"):
        renk_sayisi = len([c for c in d["color_codes"].split(",") if c.strip()])
        dolu["Renk Sayısı"] = f"{renk_sayisi} renk"
    dolu["İnternet"] = "Aktif" if d.get("internet_aktif") else "Pasif"
    if d.get("bloke"):              dolu["Durum"] = "Bloke"

    d["dolu_alanlar"] = dolu
    return d


# ── GET /scorelist — Marka > Sezon > Kategori hiyerarşisi ────────────────────

@router.get("/scorelist")
async def get_scorelist(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> List[Dict[str, Any]]:
    """Marka bazlı özet — Sezon/Kategori detayları lazy load."""
    rows = (await session.execute(text("""
        SELECT
            p.marka_adi,
            COUNT(DISTINCT p.urun_kodu)            AS toplam_urun,
            COUNT(DISTINCT p.sezon_kodu)           AS sezon_sayisi,
            ROUND(AVG(e.quality_score)::numeric,1) AS ort_skor,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='A') AS grade_a,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='B') AS grade_b,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='C') AS grade_c,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='D') AS grade_d,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='F') AS grade_f,
            COUNT(e.urun_kodu) AS puanlanan
        FROM pim_products p
        LEFT JOIN enrichment_quality e ON e.urun_kodu = p.urun_kodu
        WHERE p.marka_adi IS NOT NULL
        GROUP BY p.marka_adi
        ORDER BY toplam_urun DESC
    """))).mappings().all()
    return [dict(r) for r in rows]


@router.get("/scorelist/{marka}")
async def get_scorelist_brand(
    marka: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> List[Dict[str, Any]]:
    """Marka bazlı sezonlar."""
    rows = (await session.execute(text("""
        SELECT
            p.sezon_kodu, p.sezon_adi,
            COUNT(DISTINCT p.urun_kodu)            AS toplam_urun,
            ROUND(AVG(e.quality_score)::numeric,1) AS ort_skor,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='A') AS grade_a,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='B') AS grade_b,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='C') AS grade_c,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='D') AS grade_d,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='F') AS grade_f
        FROM pim_products p
        LEFT JOIN enrichment_quality e ON e.urun_kodu = p.urun_kodu
        WHERE p.marka_adi = :marka
        GROUP BY p.sezon_kodu, p.sezon_adi
        ORDER BY p.sezon_kodu DESC
    """), {"marka": marka})).mappings().all()
    return [dict(r) for r in rows]


@router.get("/scorelist/{marka}/{sezon}")
async def get_scorelist_season(
    marka: str,
    sezon: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> List[Dict[str, Any]]:
    """Sezon > Kategori (ana_grup_adi)."""
    rows = (await session.execute(text("""
        SELECT
            COALESCE(p.ana_grup_adi, 'Diğer')      AS kategori,
            COUNT(DISTINCT p.urun_kodu)            AS toplam_urun,
            ROUND(AVG(e.quality_score)::numeric,1) AS ort_skor,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='A') AS grade_a,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='B') AS grade_b,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='C') AS grade_c,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='D') AS grade_d,
            COUNT(e.urun_kodu) FILTER (WHERE e.quality_grade='F') AS grade_f
        FROM pim_products p
        LEFT JOIN enrichment_quality e ON e.urun_kodu = p.urun_kodu
        WHERE p.marka_adi = :marka AND p.sezon_kodu = :sezon
        GROUP BY p.ana_grup_adi
        ORDER BY toplam_urun DESC
    """), {"marka": marka, "sezon": sezon})).mappings().all()
    return [dict(r) for r in rows]


@router.get("/scorelist/{marka}/{sezon}/{kategori}")
async def get_scorelist_category(
    marka: str,
    sezon: str,
    kategori: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> List[Dict[str, Any]]:
    """Kategori > Ürünler."""
    rows = (await session.execute(text("""
        SELECT
            p.urun_kodu, p.urun_adi, p.default_image_url,
            e.quality_score, e.quality_grade,
            e.score_temel_bilgi, e.score_kumas_bilgi,
            e.score_gorsel, e.score_satis_icerik,
            jsonb_array_length(COALESCE(e.eksik_alanlar,'[]'::jsonb)) AS eksik_sayisi
        FROM pim_products p
        LEFT JOIN enrichment_quality e ON e.urun_kodu = p.urun_kodu
        WHERE p.marka_adi = :marka
          AND p.sezon_kodu = :sezon
          AND COALESCE(p.ana_grup_adi, 'Diğer') = :kat
        ORDER BY e.quality_score ASC NULLS LAST
        LIMIT 200
    """), {"marka": marka, "sezon": sezon, "kat": kategori})).mappings().all()
    return [dict(r) for r in rows]
