"""FastAPI application entry point.

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.connectors.registry import registry
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.middleware.agent_guard import AgentGuardMiddleware
from app.services import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log = get_logger(__name__)
    log.info("app.startup", env=settings.app_env, bedrock_model=settings.bedrock_model_id)
    registry.load()
    scheduler.start()
    # Hikaye yazıcı tablosunu oluştur (idempotent)
    from app.db.session import engine
    from sqlalchemy import text as _text
    async def _ddl(sql: str) -> None:
        """Her DDL'i kendi transaction'ında çalıştır, hata olursa logla ve geç."""
        try:
            async with engine.begin() as _c:
                await _c.execute(_text(sql))
        except Exception as _e:
            log.warning("ddl.failed", error=str(_e), sql_preview=sql[:80])

    await _ddl("""
        CREATE TABLE IF NOT EXISTS product_stories (
            id SERIAL PRIMARY KEY,
            urun_kodu TEXT NOT NULL,
            urun_adi  TEXT,
            marka_adi TEXT,
            kanal     TEXT NOT NULL,
            ton       TEXT NOT NULL DEFAULT 'sade_net',
            story     TEXT NOT NULL,
            karakter_sayisi INTEGER,
            durum     TEXT NOT NULL DEFAULT 'taslak',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            approved_at TIMESTAMPTZ
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_ps_urun_kod ON product_stories(urun_kodu)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_ps_kanal    ON product_stories(kanal)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_ps_durum    ON product_stories(durum)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS incorta_magaza_performans (
            id          SERIAL PRIMARY KEY,
            yil         INTEGER NOT NULL,
            ay          INTEGER NOT NULL,
            bolge_muduru TEXT,
            magaza      TEXT NOT NULL,
            hedef       DOUBLE PRECISION DEFAULT 0,
            net_ciro    DOUBLE PRECISION DEFAULT 0,
            hedef_orani DOUBLE PRECISION DEFAULT 0,
            ziyaretci   DOUBLE PRECISION DEFAULT 0,
            mdo         DOUBLE PRECISION DEFAULT 0,
            sepet       DOUBLE PRECISION DEFAULT 0,
            obf         DOUBLE PRECISION DEFAULT 0,
            net_adet    DOUBLE PRECISION DEFAULT 0,
            sync_updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_imp_yil_ay  ON incorta_magaza_performans(yil, ay)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_imp_bolge   ON incorta_magaza_performans(bolge_muduru)")
    await _ddl("CREATE UNIQUE INDEX IF NOT EXISTS idx_imp_uniq ON incorta_magaza_performans(yil, ay, bolge_muduru, magaza)")
    await _ddl("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS mv_magaza_satis_ozet AS
        SELECT
            yil::integer AS yil,
            ay::integer  AS ay,
            bolge_muduru,
            magaza,
            SUM(CASE WHEN hedef::text NOT IN ('--','') THEN hedef::float ELSE 0 END) AS toplam_hedef,
            SUM(CASE WHEN net_ciro::text NOT IN ('--','') THEN net_ciro::float ELSE 0 END) AS toplam_ciro,
            SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END) AS toplam_ziyaretci,
            SUM(CASE WHEN net_adet::text NOT IN ('--','') THEN net_adet::float ELSE 0 END) AS toplam_adet,
            CASE WHEN SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END) > 0
                 THEN SUM(CASE WHEN mdo::text NOT IN ('--','') THEN mdo::float ELSE 0 END
                          * CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END)
                      / SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END)
                 ELSE 0 END AS ort_mdo,
            CASE WHEN SUM(CASE WHEN net_adet::text NOT IN ('--','') THEN net_adet::float ELSE 0 END) > 0
                 THEN SUM(CASE WHEN net_ciro::text NOT IN ('--','') THEN net_ciro::float ELSE 0 END)
                      / SUM(CASE WHEN net_adet::text NOT IN ('--','') THEN net_adet::float ELSE 0 END)
                 ELSE 0 END AS ort_obf,
            CASE WHEN SUM(CASE WHEN hedef::text NOT IN ('--','') THEN hedef::float ELSE 0 END) > 0
                 THEN SUM(CASE WHEN net_ciro::text NOT IN ('--','') THEN net_ciro::float ELSE 0 END)
                      / SUM(CASE WHEN hedef::text NOT IN ('--','') THEN hedef::float ELSE 0 END)
                 ELSE 0 END AS hedef_oran
        FROM incorta_magaza_performans
        WHERE magaza IS NOT NULL AND TRIM(magaza) <> ''
        GROUP BY yil::integer, ay::integer, bolge_muduru, magaza
        WITH DATA
    """)
    await _ddl("CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_mag_pk ON mv_magaza_satis_ozet (yil, ay, bolge_muduru, magaza)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS siralama_gecmisi (
            id          SERIAL PRIMARY KEY,
            job_id      TEXT NOT NULL UNIQUE,
            sezon_kodu  TEXT NOT NULL,
            marka_adi   TEXT NOT NULL,
            kategori    TEXT NOT NULL,
            toplam_urun INTEGER NOT NULL DEFAULT 0,
            onayli      BOOLEAN NOT NULL DEFAULT FALSE,
            onay_tarihi TIMESTAMPTZ,
            onaylayan   TEXT,
            siralama_json JSONB NOT NULL DEFAULT '[]',
            ozet_json     JSONB,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_sir_job    ON siralama_gecmisi(job_id)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_sir_sezon  ON siralama_gecmisi(sezon_kodu, marka_adi, kategori)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS product_vision_attrs (
            id                  SERIAL PRIMARY KEY,
            urun_kodu           TEXT NOT NULL,
            urun_adi            TEXT,
            fabric_pattern_name TEXT,
            arm_length_name     TEXT,
            collar_type_name    TEXT,
            product_length_name TEXT,
            cutting_name        TEXT,
            belt_length_name    TEXT,
            fit_name            TEXT,
            thickness_type_name TEXT,
            style_name          TEXT,
            ecom_tag3_name      TEXT,
            ecom_tag4_name      TEXT,
            guven_skoru         FLOAT,
            notlar              TEXT,
            gorsel_sayisi       INTEGER,
            uyusmazlik_json     JSONB NOT NULL DEFAULT '[]',
            eksik_json          JSONB NOT NULL DEFAULT '[]',
            durum               TEXT NOT NULL DEFAULT 'taslak',
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            approved_at         TIMESTAMPTZ
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_pva_urun_kodu ON product_vision_attrs(urun_kodu)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_pva_durum     ON product_vision_attrs(durum)")
    # ── Daily Brief tabloları ─────────────────────────────────────────────────
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_profiles (
            id               SERIAL PRIMARY KEY,
            profile_id       TEXT UNIQUE NOT NULL,
            name             TEXT NOT NULL,
            role             TEXT,
            owner_email      TEXT,
            timezone         TEXT DEFAULT 'Europe/Istanbul',
            schedule_time    TIME DEFAULT '06:00',
            active_days      JSONB DEFAULT '[1,2,3,4,5]',
            is_active        BOOLEAN DEFAULT true,
            send_email       BOOLEAN DEFAULT true,
            tone             TEXT DEFAULT 'yonetici',
            length           TEXT DEFAULT 'ozet',
            format           TEXT DEFAULT 'mixed',
            top_insight_count INTEGER DEFAULT 3,
            tenant_id        TEXT NOT NULL DEFAULT 'upagon',
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            updated_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bp_tenant ON brief_profiles(tenant_id, is_active)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_questions (
            id               SERIAL PRIMARY KEY,
            profile_id       INTEGER NOT NULL REFERENCES brief_profiles(id) ON DELETE CASCADE,
            question_text    TEXT NOT NULL,
            agent            TEXT NOT NULL,
            importance       TEXT DEFAULT 'orta',
            is_cross_domain  BOOLEAN DEFAULT false,
            trigger_days     JSONB,
            trigger_dates    TEXT,
            sort_order       INTEGER DEFAULT 0,
            is_active        BOOLEAN DEFAULT true,
            created_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bq_profile ON brief_questions(profile_id, sort_order)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_question_library (
            id               SERIAL PRIMARY KEY,
            category         TEXT NOT NULL,
            question_text    TEXT NOT NULL,
            agent            TEXT NOT NULL,
            importance       TEXT DEFAULT 'orta',
            is_cross_domain  BOOLEAN DEFAULT false,
            description      TEXT,
            usage_count      INTEGER DEFAULT 0,
            is_active        BOOLEAN DEFAULT true
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bql_cat ON brief_question_library(category, is_active)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_checklist_items (
            id               SERIAL PRIMARY KEY,
            profile_id       INTEGER NOT NULL REFERENCES brief_profiles(id) ON DELETE CASCADE,
            text             TEXT NOT NULL,
            priority         TEXT DEFAULT 'med',
            trigger_rule     TEXT,
            sort_order       INTEGER DEFAULT 0,
            is_active        BOOLEAN DEFAULT true,
            created_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_history (
            id               SERIAL PRIMARY KEY,
            profile_id       INTEGER NOT NULL REFERENCES brief_profiles(id),
            brief_date       DATE NOT NULL,
            generated_at     TIMESTAMPTZ DEFAULT NOW(),
            generation_ms    INTEGER,
            top_insights     JSONB,
            kpi_data         JSONB,
            qa_results       JSONB,
            checklist_state  JSONB,
            actions          JSONB,
            agent_metadata   JSONB,
            estimated_cost   NUMERIC(8,4),
            tenant_id        TEXT NOT NULL DEFAULT 'upagon',
            UNIQUE (profile_id, brief_date)
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bh_lookup ON brief_history(profile_id, brief_date DESC)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_checklist_state (
            id               SERIAL PRIMARY KEY,
            profile_id       INTEGER NOT NULL REFERENCES brief_profiles(id) ON DELETE CASCADE,
            item_id          INTEGER NOT NULL REFERENCES brief_checklist_items(id),
            check_date       DATE NOT NULL,
            is_done          BOOLEAN DEFAULT false,
            done_at          TIMESTAMPTZ,
            UNIQUE (profile_id, item_id, check_date)
        )
    """)
    # Soru kütüphanesini seed et (boşsa)
    async def _seed_question_library() -> None:
        try:
            async with engine.begin() as _sc:
                cnt = (await _sc.execute(_text("SELECT COUNT(*) FROM brief_question_library"))).scalar()
                if cnt and cnt > 0:
                    return
                seed_sql = """
INSERT INTO brief_question_library (category, question_text, agent, importance, is_cross_domain, description) VALUES
('satis','Dün toplam ciro hedefin neresinde? Hangi bölgeler hedefi aştı, hangileri altında kaldı?','satis','kritik',false,'Günlük hedef tutturma + bölge kırılımı'),
('satis','Mağaza ziyaretçi ve MDO trendi son 7 gün nasıl? Düşüş gösteren mağazaların ortak özellikleri?','satis','yuksek',false,'Mağaza dönüşüm trend analizi'),
('satis','Bu ayın en iyi ve en kötü 5 mağazası hangileri?','satis','orta',false,'Performans sıralaması'),
('satis','OBF en yüksek hangi mağazada, nedeni nedir?','satis','orta',false,'Sepet büyüklüğü analizi'),
('satis','Bölge müdürü bazında performans sıralaması?','satis','orta',false,'Yönetici karne'),
('satis','Hangi mağazalarda ziyaretçi var ama dönüşüm yok?','satis','yuksek',false,'Kayıp fırsat tespiti'),
('eticaret','E-ticarette dünkü dönüşüm oranı geçen haftaya göre nasıl? Hangi kanallar öne çıkıyor?','eticaret','yuksek',false,'Günlük e-ticaret performans'),
('eticaret','En çok satan 10 ürün hangileri, hangi kanalda?','eticaret','orta',false,'Top performers'),
('eticaret','İade oranı en yüksek 5 ürün ve beden dağılımı?','eticaret','yuksek',false,'İade analizi'),
('eticaret','Hangi kanalın iade oranı diğerlerine göre anormal?','eticaret','yuksek',false,'Kanal kıyaslama'),
('eticaret','Sepet terk oranı son 7 günde nasıl gidiyor?','eticaret','orta',false,'Funnel analizi'),
('eticaret','Trafik kaynaklarına göre dönüşüm farkı?','eticaret','orta',false,'Marketing ROI'),
('urun','A-grade ürünlerden stok seviyesi kritik olanlar hangileri?','urun_yonetimi','orta',false,'Stok kritik uyarısı'),
('urun','Bu hafta enrichment yapılan ürün sayısı ve grade dağılımı?','urun_yonetimi','dusuk',false,'Enrichment ilerlemesi'),
('urun','Grade F ürünlerden bu hafta zenginleştirilecekler?','urun_yonetimi','orta',false,'İş listesi'),
('urun','Sıralama değişikliği yapılan ürünler ve performans etkisi?','urun_yonetimi','orta',false,'Sıralama etkisi'),
('urun','Yeni eklenen ürünler ve enrichment durumu?','urun_yonetimi','orta',false,'Yeni ürün takibi'),
('cross','İade oranı yüksek ürünlerin enrichment kalite puanı düşük mü? Hangi ürünlere müdahale etmeliyim?','kiyaslama','yuksek',true,'İade × Enrichment korelasyonu'),
('cross','Mağazada çok satan ama e-ticarette az satan ürünler var mı?','kiyaslama','orta',true,'Kanal asimetrisi'),
('cross','Enrichment puanı artırılan ürünlerde satış arttı mı, iade düştü mü?','kiyaslama','orta',true,'Zenginleştirme ROI'),
('cross','Görsel analizi yapılan ürünlerin satış performansı diğerlerine göre?','kiyaslama','orta',true,'Vision impact'),
('risk','Bu hafta dikkat etmem gereken en önemli 3 risk?','kiyaslama','kritik',true,'Top risk listesi'),
('risk','Anormal düşüş gösteren ürün veya mağaza var mı?','kiyaslama','yuksek',true,'Anomali tespiti'),
('risk','Beklenmeyen sapmalar (satış/iade/dönüşüm)?','kiyaslama','yuksek',true,'Sapma analizi'),
('risk','Hangi konularda karar vermem bekleniyor?','kiyaslama','kritik',true,'Karar bekleyen konular'),
('stratejik','Bu haftanın 3 önceliği ne olmalı?','kiyaslama','kritik',true,'Haftalık planlama'),
('stratejik','Geçen aya göre büyüme nerede, hangi alanlarda?','kiyaslama','yuksek',true,'Büyüme analizi'),
('stratejik','Sezon performansı planın neresinde gidiyor?','kiyaslama','yuksek',true,'Sezonsal takip'),
('stratejik','Hangi kategoriler büyüyor, hangileri daralıyor?','kiyaslama','orta',true,'Kategori trendi')
                """
                await _sc.execute(_text(seed_sql))
                log.info("brief_question_library.seeded")
        except Exception as _e:
            log.warning("brief_question_library.seed_failed", error=str(_e))
    # Mağaza satış cache'ini arka planda ısıt
    import asyncio as _asyncio
    _asyncio.create_task(_seed_question_library())
    async def _warm_magaza():
        try:
            from app.api.v1.endpoints.magaza_satis import _fetch_mcp_data
            await _fetch_mcp_data()
            log.info("magaza_satis.cache_warmed")
        except Exception as e:
            log.warning("magaza_satis.cache_warm_failed", error=str(e))
    _asyncio.create_task(_warm_magaza())
    yield
    scheduler.stop()
    log.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.app_debug,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(AgentGuardMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()
