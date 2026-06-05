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
    async with engine.begin() as conn:
        await conn.execute(_text("""
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
        """))
        await conn.execute(_text("CREATE INDEX IF NOT EXISTS idx_ps_urun_kod ON product_stories(urun_kodu)"))
        await conn.execute(_text("CREATE INDEX IF NOT EXISTS idx_ps_kanal    ON product_stories(kanal)"))
        await conn.execute(_text("CREATE INDEX IF NOT EXISTS idx_ps_durum    ON product_stories(durum)"))
        # Mağaza Performans (Incorta) tablosu
        await conn.execute(_text("""
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
        """
        ))
        await conn.execute(_text("CREATE INDEX IF NOT EXISTS idx_imp_yil_ay    ON incorta_magaza_performans(yil, ay)"))
        await conn.execute(_text("CREATE INDEX IF NOT EXISTS idx_imp_bolge     ON incorta_magaza_performans(bolge_muduru)"))
        await conn.execute(_text("CREATE UNIQUE INDEX IF NOT EXISTS idx_imp_uniq ON incorta_magaza_performans(yil, ay, bolge_muduru, magaza)"))
        # Mağaza satış özet materialized view (raporlama agent hızlı yol)
        await conn.execute(_text("""
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
        """))
        await conn.execute(_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_mag_pk "
            "ON mv_magaza_satis_ozet (yil, ay, bolge_muduru, magaza)"
        ))
        # Sıralama yönetimi tablosu
        await conn.execute(_text("""
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
        """))
        await conn.execute(_text("CREATE INDEX IF NOT EXISTS idx_sir_job    ON siralama_gecmisi(job_id)"))
        await conn.execute(_text("CREATE INDEX IF NOT EXISTS idx_sir_sezon  ON siralama_gecmisi(sezon_kodu, marka_adi, kategori)"))
    # Mağaza satış cache'ini arka planda ısıt
    import asyncio as _asyncio
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
