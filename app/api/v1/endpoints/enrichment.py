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


# ── GET /overview — Tüm katalog özeti ────────────────────────────────────────

@router.get("/overview")
async def get_overview(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> Dict[str, Any]:
    """Tüm puanlanmış ürünlerin genel özeti (sadece internet_aktif)."""

    # Genel istatistikler
    stats = (await session.execute(text("""
        SELECT
            COUNT(*)                                          AS toplam_sku,
            ROUND(AVG(e.quality_score)::numeric, 1)          AS ortalama_puan,
            COUNT(*) FILTER (WHERE e.quality_grade IN ('A','B')) AS iyi_kalite,
            COUNT(*) FILTER (WHERE e.quality_grade IN ('D','F')) AS kritik,
            COUNT(*) FILTER (WHERE e.quality_grade='A')       AS grade_a,
            COUNT(*) FILTER (WHERE e.quality_grade='B')       AS grade_b,
            COUNT(*) FILTER (WHERE e.quality_grade='C')       AS grade_c,
            COUNT(*) FILTER (WHERE e.quality_grade='D')       AS grade_d,
            COUNT(*) FILTER (WHERE e.quality_grade='F')       AS grade_f
        FROM enrichment_quality e
        JOIN pim_products p ON p.urun_kodu = e.urun_kodu
        WHERE p.internet_aktif = true
    """))).mappings().first()

    if not stats or not stats["toplam_sku"]:
        # Mock data — henüz puanlama yok
        return {
            "toplam_sku": 0, "ortalama_puan": 0,
            "iyi_kalite_pct": 0, "kritik_pct": 0,
            "grade_dagilimi": {"A":0,"B":0,"C":0,"D":0,"F":0},
            "puan_dagilimi": [{"aralik":f"{i}-{i+10}","sayi":0} for i in range(0,100,10)],
            "top_eksiklikler": [], "sezon_ozeti": [],
        }

    total = stats["toplam_sku"] or 1

    # Puan dağılımı
    dist = (await session.execute(text("""
        SELECT (e.quality_score / 10) * 10 AS bas, COUNT(*) AS sayi
        FROM enrichment_quality e
        JOIN pim_products p ON p.urun_kodu = e.urun_kodu
        WHERE p.internet_aktif = true
        GROUP BY bas ORDER BY bas
    """))).mappings().all()
    dist_map = {r["bas"]: r["sayi"] for r in dist}
    puan_dagilimi = [{"aralik":f"{i}-{i+10}", "sayi": dist_map.get(i,0)} for i in range(0,100,10)]

    # Top eksiklikler
    eksik_rows = (await session.execute(text("""
        SELECT eksik_alan, COUNT(*) AS sayi
        FROM enrichment_quality e
        JOIN pim_products p ON p.urun_kodu = e.urun_kodu
        CROSS JOIN LATERAL jsonb_array_elements_text(e.eksik_alanlar) AS eksik_alan
        WHERE p.internet_aktif = true
        GROUP BY eksik_alan ORDER BY sayi DESC LIMIT 10
    """))).mappings().all()
    top_eksiklikler = [
        {"alan": r["eksik_alan"], "sayi": r["sayi"], "pct": round(r["sayi"]/total*100)}
        for r in eksik_rows
    ]

    # Sezon özeti
    sezon_rows = (await session.execute(text("""
        SELECT
            e.sezon_kodu, MAX(e.sezon_adi) AS sezon_adi,
            ROUND(AVG(e.quality_score)::numeric,1) AS ortalama_puan,
            COUNT(*) AS toplam_urun
        FROM enrichment_quality e
        JOIN pim_products p ON p.urun_kodu = e.urun_kodu
        WHERE p.internet_aktif = true AND e.sezon_kodu IS NOT NULL
        GROUP BY e.sezon_kodu
        ORDER BY e.sezon_kodu DESC
        LIMIT 8
    """))).mappings().all()

    def _grade(v): return "A" if v>=90 else "B" if v>=75 else "C" if v>=60 else "D" if v>=40 else "F"

    sezon_ozeti = [
        {
            "sezon_kodu": r["sezon_kodu"],
            "sezon_adi":  r["sezon_adi"] or r["sezon_kodu"],
            "ortalama_puan": float(r["ortalama_puan"] or 0),
            "quality_grade": _grade(float(r["ortalama_puan"] or 0)),
            "toplam_urun": r["toplam_urun"],
            "scored": True,
        }
        for r in sezon_rows
    ]

    return {
        "toplam_sku":      total,
        "ortalama_puan":   float(stats["ortalama_puan"] or 0),
        "iyi_kalite_pct":  round(stats["iyi_kalite"] / total * 100),
        "kritik_pct":      round(stats["kritik"] / total * 100),
        "iyi_kalite_sayi": stats["iyi_kalite"],
        "kritik_sayi":     stats["kritik"],
        "grade_dagilimi":  {
            "A": stats["grade_a"], "B": stats["grade_b"],
            "C": stats["grade_c"], "D": stats["grade_d"],
            "F": stats["grade_f"],
        },
        "puan_dagilimi":   puan_dagilimi,
        "top_eksiklikler": top_eksiklikler,
        "sezon_ozeti":     sezon_ozeti,
    }


# ── Story Writer endpoints ─────────────────────────────────────────────────────

_TON_PROMPTS = {
    "lüks_zarif":   "Lüks, zarif ve sofistike bir ton kullan. Kaliteyi ve şıklığı ön plana çıkar.",
    "genç_enerjik": "Genç, enerjik ve dinamik bir ton kullan. Trendy, canlı ve heyecan verici bir dil seç.",
    "sade_net":     "Sade, net ve anlaşılır bir ton kullan. Doğrudan özellik odaklı ol, gereksiz süsleme yapma.",
    "profesyonel":  "Profesyonel, güvenilir ve otoriter bir ton kullan. Ürünün işlevselliğini ve kalitesini vurgula.",
}
_KANAL_LIMITS = {"trendyol": 200, "hepsiburada": 180, "amazon": 200, "web_sitesi": 500, "magaza": 500}


@router.get("/story/products")
async def get_story_products(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    sezon: Optional[str] = Query(None),
    marka: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(40, ge=1, le=100),
) -> Dict[str, Any]:
    """Hikaye yazıcı için ürün listesi."""
    conditions = ["p.internet_aktif = true"]
    params: Dict[str, Any] = {"offset": (page - 1) * limit, "limit": limit}
    if sezon:   conditions.append("p.sezon_kodu = :sezon");  params["sezon"] = sezon
    if marka:   conditions.append("p.marka_adi ILIKE :marka"); params["marka"] = f"%{marka}%"
    if q:       conditions.append("(p.urun_adi ILIKE :q OR p.urun_kodu ILIKE :q)"); params["q"] = f"%{q}%"
    where = " AND ".join(conditions)

    total = (await session.execute(text(
        f"SELECT COUNT(*) FROM pim_products p WHERE {where}"
    ), params)).scalar() or 0

    rows = (await session.execute(text(f"""
        SELECT p.urun_kodu, p.urun_adi, p.marka_adi, p.sezon_kodu, p.sezon_adi,
               p.ana_grup_adi, p.urun_grubu_adi, p.fabricmaterialname,
               p.color_codes, p.first_color_code, p.tema_adi, p.default_image_url,
               eq.quality_grade, eq.quality_score
        FROM pim_products p
        LEFT JOIN enrichment_quality eq ON eq.urun_kodu = p.urun_kodu
        WHERE {where}
        ORDER BY p.sezon_kodu DESC, p.urun_adi
        LIMIT :limit OFFSET :offset
    """), params)).mappings().all()

    return {"total": total, "page": page, "limit": limit, "items": [dict(r) for r in rows]}


@router.get("/story/for-product/{urun_kodu}")
async def get_product_stories(
    urun_kodu: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> List[Dict[str, Any]]:
    """Bir ürünün tüm kayıtlı hikayelerini getirir (kanal bazlı)."""
    rows = (await session.execute(text("""
        SELECT id, urun_kodu, kanal, ton, story, karakter_sayisi, durum,
               created_at, approved_at
        FROM product_stories
        WHERE urun_kodu = :uk
        ORDER BY kanal, created_at DESC
    """), {"uk": urun_kodu})).mappings().all()
    result = []
    for r in rows:
        d = dict(r)
        d["created_at"]  = d["created_at"].isoformat() if d.get("created_at") else None
        d["approved_at"] = d["approved_at"].isoformat() if d.get("approved_at") else None
        result.append(d)
    return result


@router.post("/story/generate")
async def generate_story(
    body: Dict[str, Any],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> List[Dict[str, Any]]:
    """Her ürün × kanal çifti için hikaye üret, DB'e taslak olarak kaydet."""
    from app.agent.llm_client import LLMClient

    urun_kodlari: List[str] = body.get("urun_kodlari", [])
    kanallar:     List[str] = body.get("kanallar", []) or ["trendyol"]
    ton:          str       = body.get("ton", "sade_net")
    karakter_limit: int     = int(body.get("karakter_limit", 150))
    karakter_min            = max(80, karakter_limit - 30)

    if not urun_kodlari:
        return []

    prod_rows = (await session.execute(text("""
        SELECT urun_kodu, urun_adi, marka_adi, sezon_adi,
               ana_grup_adi, urun_grubu_adi, fabricmaterialname, color_codes, tema_adi
        FROM pim_products WHERE urun_kodu = ANY(:kodlar)
    """), {"kodlar": urun_kodlari})).mappings().all()

    ton_str = _TON_PROMPTS.get(ton, _TON_PROMPTS["sade_net"])
    llm = LLMClient()
    results = []

    for row in prod_rows:
        p = dict(row)
        renk_sayisi = len([c for c in (p.get("color_codes") or "").split(",") if c.strip()])
        urun_bilgi = (
            f"Ürün Adı: {p['urun_adi']}\n"
            f"Kategori: {p.get('ana_grup_adi','')} / {p.get('urun_grubu_adi','')}\n"
            f"Materyal: {p.get('fabricmaterialname','')}\n"
            f"Tema: {p.get('tema_adi','')}\n"
        )
        if renk_sayisi > 0:
            urun_bilgi += f"Renk Seçeneği: {renk_sayisi} farklı renk\n"

        for kanal in kanallar:
            eff_limit = min(karakter_limit, _KANAL_LIMITS.get(kanal, 500))
            user_prompt = (
                f"Aşağıdaki ürün için satış artırıcı bir açıklama yaz.\n\n"
                f"{urun_bilgi}\n"
                f"Hedef kanal: {kanal}\n"
                f"Karakter limiti: {karakter_min}–{eff_limit} karakter arası olmalı.\n"
                "Sadece açıklama metnini döndür, başka hiçbir şey yazma."
            )
            system_prompt = (
                "Sen deneyimli bir e-ticaret satış temsilcisin. "
                "Görevin ürün özellikleri verildiğinde, müşterilerin satın alma kararını "
                f"destekleyecek kısa, etkili Türkçe açıklama yazmak. {ton_str} "
                "Sadece açıklama metni — başlık, madde işareti yok."
            )
            try:
                story = await llm.complete(
                    system=system_prompt, user=user_prompt, max_tokens=300, temperature=0.8
                )
                story = story.strip().strip('"').strip()
            except Exception as e:
                log.error("story.generate_error", urun_kodu=p["urun_kodu"], kanal=kanal, error=str(e))
                story = ""

            # DB'e kaydet — varsa güncelle, yoksa ekle
            existing = (await session.execute(text("""
                SELECT id FROM product_stories
                WHERE urun_kodu=:uk AND kanal=:kanal AND durum='taslak'
                ORDER BY created_at DESC LIMIT 1
            """), {"uk": p["urun_kodu"], "kanal": kanal})).scalar()

            if existing:
                await session.execute(text("""
                    UPDATE product_stories
                    SET story=:story, karakter_sayisi=:ks, ton=:ton, created_at=NOW()
                    WHERE id=:id
                """), {"story": story, "ks": len(story), "ton": ton, "id": existing})
                story_id = existing
            else:
                story_id = (await session.execute(text("""
                    INSERT INTO product_stories
                        (urun_kodu, urun_adi, marka_adi, kanal, ton, story, karakter_sayisi, durum)
                    VALUES (:uk, :ua, :ma, :kanal, :ton, :story, :ks, 'taslak')
                    RETURNING id
                """), {
                    "uk": p["urun_kodu"], "ua": p["urun_adi"], "ma": p["marka_adi"],
                    "kanal": kanal, "ton": ton, "story": story, "ks": len(story),
                })).scalar()

            results.append({
                "id": story_id, "urun_kodu": p["urun_kodu"],
                "kanal": kanal, "story": story, "karakter_sayisi": len(story),
                "durum": "taslak",
            })

    await session.commit()
    return results


@router.post("/story/{story_id}/approve")
async def approve_story(
    story_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Dict[str, Any]:
    """Hikayeyi onayla — durum → onaylandi."""
    result = await session.execute(text("""
        UPDATE product_stories
        SET durum='onaylandi', approved_at=NOW()
        WHERE id=:id
        RETURNING id, urun_kodu, kanal, story, durum, approved_at
    """), {"id": story_id})
    row = result.mappings().first()
    await session.commit()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, f"Story #{story_id} bulunamadı")
    d = dict(row)
    d["approved_at"] = d["approved_at"].isoformat() if d.get("approved_at") else None
    return d


@router.delete("/story/{story_id}")
async def delete_story(
    story_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Dict[str, Any]:
    """Hikayeyi sil."""
    await session.execute(text("DELETE FROM product_stories WHERE id=:id"), {"id": story_id})
    await session.commit()
    return {"deleted": story_id}


@router.get("/story/saved")
async def get_saved_stories(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    kanal: Optional[str] = Query(None),
    durum: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
) -> Dict[str, Any]:
    """DB'deki tüm hikayeler — arşiv sayfası için."""
    conditions: List[str] = []
    params: Dict[str, Any] = {"offset": (page - 1) * limit, "limit": limit}
    if kanal: conditions.append("ps.kanal=:kanal");  params["kanal"] = kanal
    if durum: conditions.append("ps.durum=:durum");  params["durum"] = durum
    if q:     conditions.append("(ps.urun_adi ILIKE :q OR ps.urun_kodu ILIKE :q OR ps.story ILIKE :q)"); params["q"] = f"%{q}%"
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = (await session.execute(text(
        f"SELECT COUNT(*) FROM product_stories ps {where}"
    ), params)).scalar() or 0

    rows = (await session.execute(text(f"""
        SELECT ps.id, ps.urun_kodu, ps.urun_adi, ps.marka_adi,
               ps.kanal, ps.ton, ps.story, ps.karakter_sayisi,
               ps.durum, ps.created_at, ps.approved_at
        FROM product_stories ps
        {where}
        ORDER BY ps.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)).mappings().all()

    items = []
    for r in rows:
        d = dict(r)
        d["created_at"]  = d["created_at"].isoformat() if d.get("created_at") else None
        d["approved_at"] = d["approved_at"].isoformat() if d.get("approved_at") else None
        items.append(d)

    return {"total": total, "page": page, "limit": limit, "items": items}


@router.get("/overview/by-field")
async def get_overview_by_field(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> List[Dict[str, Any]]:
    """Alan bazlı eksiklik detayı."""
    total_row = (await session.execute(text("""
        SELECT COUNT(*) FROM enrichment_quality e
        JOIN pim_products p ON p.urun_kodu = e.urun_kodu
        WHERE p.internet_aktif = true
    """))).scalar() or 1

    rows = (await session.execute(text("""
        SELECT eksik_alan, COUNT(*) AS sayi
        FROM enrichment_quality e
        JOIN pim_products p ON p.urun_kodu = e.urun_kodu
        CROSS JOIN LATERAL jsonb_array_elements_text(e.eksik_alanlar) AS eksik_alan
        WHERE p.internet_aktif = true
        GROUP BY eksik_alan ORDER BY sayi DESC
    """))).mappings().all()

    return [
        {"alan": r["eksik_alan"], "sayi": r["sayi"], "pct": round(r["sayi"]/total_row*100)}
        for r in rows
    ]
