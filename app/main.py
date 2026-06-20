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
        CREATE TABLE IF NOT EXISTS incorta_magaza_gunluk (
            id          SERIAL PRIMARY KEY,
            tarih       TEXT NOT NULL,
            magaza      TEXT,
            urun_kodu   TEXT,
            urun_adi    TEXT,
            renk        TEXT,
            beden       TEXT,
            satis_tutar DOUBLE PRECISION DEFAULT 0,
            satis_adet  DOUBLE PRECISION DEFAULT 0,
            iade_tutari DOUBLE PRECISION DEFAULT 0,
            iade_adeti  DOUBLE PRECISION DEFAULT 0,
            sync_updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_img_tarih    ON incorta_magaza_gunluk(tarih)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_img_magaza   ON incorta_magaza_gunluk(magaza)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_img_tarih_mag ON incorta_magaza_gunluk(tarih, magaza)")
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
    # brief_profiles: sadece kişi/rol bilgisi (zamanlama brief_schedules'da)
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_profiles (
            id               SERIAL PRIMARY KEY,
            profile_id       TEXT UNIQUE NOT NULL,
            name             TEXT NOT NULL,
            role             TEXT,
            owner_email      TEXT,
            timezone         TEXT DEFAULT 'Europe/Istanbul',
            is_active        BOOLEAN DEFAULT true,
            tenant_id        TEXT NOT NULL DEFAULT 'upagon',
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            updated_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bp_tenant ON brief_profiles(tenant_id, is_active)")
    # brief_schedules: bir profil altında birden fazla gönderim planı
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_schedules (
            id               SERIAL PRIMARY KEY,
            profile_id       INTEGER NOT NULL REFERENCES brief_profiles(id) ON DELETE CASCADE,
            name             TEXT NOT NULL,
            frequency_type   TEXT NOT NULL DEFAULT 'daily',
            schedule_time    TIME DEFAULT '07:00',
            active_days      JSONB DEFAULT '[1,2,3,4,5]',
            send_email       BOOLEAN DEFAULT true,
            tone             TEXT DEFAULT 'yonetici',
            length           TEXT DEFAULT 'ozet',
            format           TEXT DEFAULT 'mixed',
            top_insight_count INTEGER DEFAULT 3,
            is_active        BOOLEAN DEFAULT true,
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            updated_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bs_profile ON brief_schedules(profile_id, is_active)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_questions (
            id               SERIAL PRIMARY KEY,
            schedule_id      INTEGER NOT NULL REFERENCES brief_schedules(id) ON DELETE CASCADE,
            question_text    TEXT NOT NULL,
            agent            TEXT NOT NULL,
            importance       TEXT DEFAULT 'orta',
            is_cross_domain  BOOLEAN DEFAULT false,
            trigger_days     JSONB,
            sort_order       INTEGER DEFAULT 0,
            is_active        BOOLEAN DEFAULT true,
            created_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bq_schedule ON brief_questions(schedule_id, sort_order)")
    await _ddl("""
        CREATE TABLE IF NOT EXISTS brief_question_library (
            id               SERIAL PRIMARY KEY,
            question_code    TEXT,
            category_code    TEXT,
            category         TEXT NOT NULL,
            department       TEXT NOT NULL DEFAULT 'eticaret',
            agent            TEXT NOT NULL,
            agent_label      TEXT,
            question_text    TEXT NOT NULL,
            importance       TEXT DEFAULT 'orta',
            frequency        TEXT DEFAULT 'daily',
            data_status      TEXT DEFAULT 'available',
            data_sources     JSONB DEFAULT '[]',
            constraints_note TEXT,
            is_cross_domain  BOOLEAN DEFAULT false,
            description      TEXT,
            usage_count      INTEGER DEFAULT 0,
            sort_order       INTEGER DEFAULT 0,
            is_active        BOOLEAN DEFAULT true
        )
    """)
    # Migration: mevcut tabloya yeni kolonlar ekle (IF NOT EXISTS PostgreSQL 9.6+)
    for _col in [
        "ADD COLUMN IF NOT EXISTS question_code    TEXT",
        "ADD COLUMN IF NOT EXISTS category_code    TEXT",
        "ADD COLUMN IF NOT EXISTS department       TEXT NOT NULL DEFAULT 'eticaret'",
        "ADD COLUMN IF NOT EXISTS agent_label      TEXT",
        "ADD COLUMN IF NOT EXISTS frequency        TEXT DEFAULT 'daily'",
        "ADD COLUMN IF NOT EXISTS data_status      TEXT DEFAULT 'available'",
        "ADD COLUMN IF NOT EXISTS data_sources     JSONB DEFAULT '[]'",
        "ADD COLUMN IF NOT EXISTS constraints_note TEXT",
        "ADD COLUMN IF NOT EXISTS sort_order       INTEGER DEFAULT 0",
    ]:
        await _ddl(f"ALTER TABLE brief_question_library {_col}")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bql_cat ON brief_question_library(category, is_active)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bql_dept ON brief_question_library(department, data_status)")
    # brief_checklist_items: profil düzeyinde (kişiye ait, zamanlama bağımsız)
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
            schedule_id      INTEGER REFERENCES brief_schedules(id) ON DELETE SET NULL,
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
            UNIQUE (schedule_id, brief_date)
        )
    """)
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bh_lookup ON brief_history(schedule_id, brief_date DESC)")
    await _ddl("CREATE INDEX IF NOT EXISTS idx_bh_profile ON brief_history(profile_id, brief_date DESC)")
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
    # Soru kütüphanesini seed et — 122 soru (eticaret_soru_havuzu_v2)
    async def _seed_question_library() -> None:
        import json as _json
        _S = "available"; _P = "partial"; _I = "integration_needed"
        _d = "daily"; _w = "weekly"; _m = "monthly"
        _K = "kritik"; _Y = "yuksek"; _O = "orta"
        # (code, cat_code, category, dept, agent, agent_label, text, imp, freq, status, sources, constraint, cross, sort)
        _QUESTIONS = [
            # ── A. SATIŞ PERFORMANSI ──────────────────────────────────────────
            ("A01","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Dün toplam e-ticaret brüt ve net cirası nedir? Kanal bazında kırılım ve geçen haftanın aynı günüyle karşılaştırma?",_K,_d,_S,["incorta_satis","incorta_depo_iade","incorta_iptal_siparis"],"",False,1),
            ("A02","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Dün toplam sipariş adedi ve ortalama sepet değeri nedir? Geçen aya göre trend nasıl?",_K,_d,_P,["incorta_satis"],"Sipariş ID alanı mevcut veri modelinde yok. Sipariş adedi ve sepet değeri hesaplanamıyor.",False,2),
            ("A03","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Dün en çok satan 10 ürün hangileri? Ciro ve adet bazında sıralama? Geçen haftanın aynı günüyle karşılaştırıldığında pozisyon değişimi?",_Y,_d,_S,["incorta_satis","kiyaslama"],"",False,3),
            ("A04","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Dünkü satış dağılımı nasıl? Günün toplamı beklentinin üstünde mi altında mı? Kampanya günüyse etki ölçülebiliyor mu?",_Y,_d,_P,["incorta_satis"],"Saatlik breakdown günlük granülaritede mümkün değil. Saatlik analiz isteniyorsa Incorta'dan saatlik veri kaynağı eklenmeli.",False,4),
            ("A05","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Dün en yüksek iptal oranına sahip 5 ürün hangileri? Kanallar arasında iptal oranı farkı var mı?",_Y,_d,_S,["incorta_iptal_siparis"],"",False,5),
            ("A06","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Dün en yüksek iade oranına sahip 5 ürün hangileri? Beden dağılımı nasıl?",_Y,_d,_S,["incorta_depo_iade"],"İade nedeni kodu mevcut veri modelinde tanımlı değil.",False,6),
            ("A07","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Brüt ciro ile net ciro arasındaki makas ne kadar? İade ve iptal oranları geçen haftaya göre artıyor mu, azalıyor mu?",_O,_d,_S,["incorta_satis","incorta_depo_iade","incorta_iptal_siparis"],"",False,7),
            ("A08","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Dün sıfır satış olan aktif ürünler hangileri? Teknik sorun mu, talep eksikliği mi?",_O,_d,_S,["incorta_satis","pim"],"",False,8),
            ("A09","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Bu hafta toplam ciro hedefin neresindeyiz? Hangi kanal hedefi aştı, hangisi altında kaldı?",_K,_w,_S,["incorta_satis","hedef_tablosu"],"",False,9),
            ("A10","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Geçen haftaya göre büyüme veya düşüş hangi kategoride yaşandı? Trend devam ediyor mu?",_Y,_w,_S,["incorta_satis","kiyaslama"],"",False,10),
            ("A11","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Haftalık sepet analizi: Müşteri başına ortalama ürün adedi kaç? Çapraz satış ve üst satış oranları nasıl?",_Y,_w,_I,[],"Sipariş bazlı veri gerekli (sipariş ID yok). Incorta'da sipariş bazlı tablo araştırılmalı.",False,11),
            ("A12","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Bu hafta hangi yeni ürünler ilk satışını yaptı? İlk 7 gün performansı beklentiyi karşılıyor mu?",_O,_w,_S,["incorta_satis","pim"],"",False,12),
            ("A13","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Geçen ayın toplam cirası ve yıllık hedefin neresindeyiz? Sezon başından bu yana büyüme oranı?",_K,_m,_S,["mv_net_satis_aylik","hedef_tablosu","kiyaslama"],"",False,13),
            ("A14","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Geçen yılın aynı ayıyla karşılaştırıldığında ciro, sipariş adedi ve sepet değeri nasıl değişti?",_Y,_m,_S,["mv_net_satis_aylik","kiyaslama"],"",False,14),
            ("A15","A","Satış Performansı","eticaret","eticaret","EticaretAgent","Aylık satış tahmini ile gerçekleşen arasındaki sapma nedir? Sapmanın ana nedeni nedir?",_Y,_m,_P,["mv_net_satis_aylik","hedef_tablosu"],"Tahmin modeli henüz yok. Trend bazlı basit projeksiyon yapılabilir.",False,15),
            # ── B. ÜRÜN VE KATALOG YÖNETİMİ ─────────────────────────────────
            ("B01","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Aktif ürünlerden son 7 günde hiç satışı olmayan kaç ürün var? En uzun süredir satış yapmayan 10 ürün hangileri?",_K,_d,_S,["incorta_satis","pim"],"",False,1),
            ("B02","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Stok kritik seviyeye düşen (7 günden az satış kapasitesi kalan) A ve B grade ürünler hangileri?",_Y,_d,_S,["pimland_mcp_stok","incorta_satis","enrichment_quality"],"",False,2),
            ("B03","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Dün yeni eklenen ürünler e-ticaret sitesinde doğru görünüyor mu? Görsel, fiyat, beden seçenekleri eksiksiz mi?",_Y,_d,_P,["pim"],"PIM verisi kontrol edilebilir ancak site audit (gerçek sitedeki görünüm) entegrasyonu yok.",False,3),
            ("B04","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Son 24 saatte fiyatı değişen ürünler hangileri? Fiyat değişikliği sonrası satış hareketlendi mi?",_O,_d,_I,[],"Fiyat değişim logu gerekli. pimland_sync'e price_change_log tablosu eklenebilir.",False,4),
            ("B05","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Yeni sezondaki ürünlerin yayınlanma durumu nedir? Kaç ürün hazır, kaç ürün hâlâ eksik içerikle bekliyor?",_O,_d,_S,["pim","enrichment_quality"],"",False,5),
            ("B06","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Bu hafta en çok görüntülenip satın alınmayan ürünler hangileri? Dönüşüm engelleyen etken ne olabilir?",_Y,_w,_I,[],"GA4 / site analytics entegrasyonu gerekli (sayfa görüntüleme verisi yok).",False,6),
            ("B07","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Kategori bazında dönüşüm oranları nasıl? Hangi kategoride tıklanma fazla ama satış az?",_Y,_w,_I,[],"GA4 / site analytics entegrasyonu gerekli.",False,7),
            ("B09","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Satışı düşük ve uzun süredir hareketsiz ürünler hangileri? Bu ürünler için aksiyon planı var mı?",_Y,_w,_S,["incorta_satis","pim"],"",False,8),
            ("B10","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Bu hafta fiyat indirimi uygulanan ürünlerin satış artışı beklentiyi karşıladı mı?",_O,_w,_I,[],"Fiyat değişim logu gerekli.",False,9),
            ("B11","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Sezon bazında en iyi ve en kötü performanslı 20 ürün hangileri? Bu ürünlerin ortak özellikleri neler?",_Y,_m,_S,["mv_net_satis_urun","pim"],"",False,10),
            ("B12","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Aylık olarak satışa açılan yeni ürünlerin başarı oranı nedir? İlk 30 gün içinde hedefe ulaşan ürün yüzdesi?",_Y,_m,_S,["incorta_satis","pim"],"",False,11),
            ("B13","B","Ürün ve Katalog Yönetimi","eticaret","urun_yonetimi","EticaretAgent + EnrichmentAgent","Uzun vadede katalogda yer alan ama hiç satış yapmayan ürün sayısı? Katalogdan çıkarma kararı?",_O,_m,_S,["incorta_satis","pim"],"",False,12),
            # ── C. MÜŞTERİ DENEYİMİ ─────────────────────────────────────────
            ("C01","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Dün site arama verilerine göre en çok aranan ama bulunamayan (sıfır sonuç) kelimeler neler?",_Y,_d,_I,[],"Site arama logu entegrasyonu gerekli.",False,1),
            ("C02","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Dün ödeme adımında terk edilen sepet oranı nedir? Terk edilen sepetlerin ortalama değeri nedir?",_Y,_d,_I,[],"GA4 checkout funnel entegrasyonu gerekli.",False,2),
            ("C03","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Dün gelen müşteri şikayetlerinin konuları neler? En çok tekrar eden sorun hangisi?",_O,_d,_I,[],"CRM / müşteri hizmetleri API gerekli.",False,3),
            ("C04","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Bu hafta ürün yorumlarına gelen puanlar nasıl? Düşük puan alan ürünlerin iade oranıyla korelasyonu var mı?",_Y,_w,_I,[],"Review platform API gerekli.",False,4),
            ("C05","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Müşteri başına tekrar satın alma oranı nasıl? Bu hafta kaç yeni, kaç geri dönen müşteri sipariş verdi?",_Y,_w,_I,[],"CRM + sipariş bazlı müşteri ID gerekli.",False,5),
            ("C06","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Wishlist (istek listesi) en çok eklenen ürünler hangileri? Bu ürünler stokta var mı?",_O,_w,_I,[],"Site analytics / e-ticaret platform API gerekli.",False,6),
            ("C07","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Beden kılavuzuna rağmen yanlış beden siparişi ve iade oranı yüksek ürünler hangileri? Beden rehberi güncellemesi gerekiyor mu?",_Y,_w,_P,["incorta_depo_iade","pim_beden"],"Beden bazlı iade dağılımı hesaplanabilir ancak iade nedeni=yanlış beden kodu yok.",False,7),
            ("C08","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Bu hafta site içi arama tıklama sonrası dönüşüm oranı nedir? Arama sonuçlarının kalitesi nasıl?",_O,_w,_I,[],"Site arama analytics entegrasyonu gerekli.",False,8),
            ("C09","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Aylık Net Promoter Score (NPS) veya müşteri memnuniyet skoru nasıl? Geçen aya göre nasıl değişti?",_Y,_m,_I,[],"NPS / anket aracı API gerekli.",False,9),
            ("C10","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Aylık müşteri şikayet kategorileri ve çözüm oranları nedir? En kronik sorun hangi başlıkta?",_O,_m,_I,[],"CRM API gerekli.",False,10),
            ("C11","C","Müşteri Deneyimi","eticaret","eticaret","MüşteriAgent (Yeni)","Müşteri yaşam boyu değeri (CLV) bu ay nasıl değişti? En değerli müşteri segmenti kimler?",_Y,_m,_I,[],"CRM + sipariş geçmişi API gerekli.",False,11),
            # ── D. PAZARLAMA VE KANAL YÖNETİMİ ──────────────────────────────
            ("D01","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Dün aktif kampanyaların performansı nasıl? Harcama, tıklama, dönüşüm ve ROAS kanal bazında?",_K,_d,_P,["meta_ads_mcp"],"Meta Ads MCP bağlı, aktifleştirilmeli. Google Ads ayrı entegrasyon gerekli.",False,1),
            ("D02","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Dün reklam harcaması günlük bütçenin neresinde? Bütçe aşım riski olan kampanya var mı?",_K,_d,_P,["meta_ads_mcp"],"Meta Ads MCP aktifleştirilmeli.",False,2),
            ("D03","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Dün en yüksek ROAS'ı hangi kampanya/kanal sağladı? En düşük ROAS'ı olan kampanya nedir?",_Y,_d,_P,["meta_ads_mcp"],"Meta Ads MCP aktifleştirilmeli.",False,3),
            ("D04","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Dün e-posta kampanyası gönderildiyse açılma, tıklama ve dönüşüm oranları nedir?",_Y,_d,_I,[],"E-posta pazarlama aracı API gerekli (Klaviyo / Mailchimp).",False,4),
            ("D05","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Dün sosyal medya organik paylaşımların etkileşim oranı nedir? Hangi içerik en çok ilgi gördü?",_O,_d,_I,[],"Social media analytics API gerekli.",False,5),
            ("D06","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Bu hafta trafik kaynaklarına göre dönüşüm oranı nedir? Organik, ücretli, direkt, e-posta, sosyal karşılaştırması?",_Y,_w,_P,["incorta_analytics"],"Genel kanal bazlı mevcut, ancak kampanya bazlı detay sınırlı.",False,6),
            ("D07","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Bu hafta influencer içeriklerinin performansı nedir? Yönlendirme trafiği ve satış katkısı ölçülebildi mi?",_Y,_w,_I,[],"UTM takibi + GA4 entegrasyonu gerekli.",False,7),
            ("D08","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Haftalık reklam harcaması kategoriye göre dağılımı doğru mu? En yüksek ROAS'lı kategoriye yeterli bütçe ayrılıyor mu?",_Y,_w,_I,[],"Ads API + kategori bazlı eşleştirme gerekli.",False,8),
            ("D09","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Bu hafta retargeting kampanyaları terk edilen sepet kurtarma oranı nedir?",_O,_w,_I,[],"Ads API + GA4 entegrasyonu gerekli.",False,9),
            ("D10","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Pazar yeri (Trendyol, HB, Amazon vb.) reklam ve organik görünürlük performansı bu hafta nasıl?",_O,_w,_I,[],"Pazar yeri seller panel API gerekli.",False,10),
            ("D11","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Geçen ay toplam pazarlama harcaması nedir? Kanal bazında planlanan vs gerçekleşen bütçe sapması?",_K,_m,_P,["meta_ads_mcp"],"Meta Ads MCP aktifleştirilmeli + bütçe tablosu manuel.",False,11),
            ("D12","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Aylık müşteri edinme maliyeti (CAC) kanal bazında nedir? Hangi kanal en verimli müşteri getiriyor?",_K,_m,_I,[],"Ads API + CRM entegrasyonu gerekli.",False,12),
            ("D13","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Aylık e-posta listesi büyümesi ve abonelikten çıkma oranı nedir?",_Y,_m,_I,[],"E-posta pazarlama aracı API gerekli.",False,13),
            ("D14","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Geçen ayki kampanyaların başarı değerlendirmesi: Hedeflenen vs gerçekleşen ROAS, dönüşüm, gelir?",_Y,_m,_I,[],"Ads API gerekli.",False,14),
            ("D15","D","Pazarlama ve Kanal Yönetimi","eticaret","eticaret","PazarlamaAgent (Yeni)","Aylık SEO performansı: Organik trafik artışı, sıralama kazanımları, kayıplar ve öne çıkan arama terimleri?",_O,_m,_I,[],"Google Search Console API gerekli.",False,15),
            # ── E. OPERASYON VE LOJİSTİK ─────────────────────────────────────
            ("E01","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Dün gelen siparişlerden kargo teslim süreleri hedefte mi? Geciken sipariş var mı, nedeni nedir?",_K,_d,_I,[],"Kargo/lojistik API gerekli.",False,1),
            ("E02","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Dün iade edilen ürünler depoya ulaştı mı? Hatalı iade (yanlış ürün gönderilmiş) oranı nedir?",_Y,_d,_I,[],"İade API + lojistik API gerekli.",False,2),
            ("E03","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Dün sipariş işlem süresi (sipariş → kargoya teslim) hedef içinde mi?",_Y,_d,_I,[],"OMS (Sipariş Yönetim Sistemi) gerekli.",False,3),
            ("E04","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Stok tutarsızlığı uyarısı var mı? Sistem stokla fiziksel stok arasında fark olan ürünler?",_O,_d,_P,["pimland_mcp_stok"],"Sistem stoku görülebilir ancak fiziksel stok karşılaştırması için WMS/ERP gerekli.",False,4),
            ("E05","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Bu hafta ortalama kargo teslimat süresi nedir? Kargo firması bazında karşılaştırma?",_Y,_w,_I,[],"Kargo API gerekli.",False,5),
            ("E06","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Bu hafta iade işlemlerinin tamamlanma süresi hedefte mi? Müşteriye geri ödeme yapılma süresi nedir?",_Y,_w,_I,[],"İade API + ödeme sistemi gerekli.",False,6),
            ("E07","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Bu hafta depo kapasitesi ve doluluk oranı nedir? Yaklaşan kampanya için depo hazırlığı yeterli mi?",_O,_w,_I,[],"WMS (Depo Yönetim Sistemi) gerekli.",False,7),
            ("E08","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Bu hafta kargo hasarı veya kayıp gönderi şikayetleri kaç adet?",_O,_w,_I,[],"CRM + Kargo API gerekli.",False,8),
            ("E09","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Aylık lojistik maliyet analizi: Sipariş başına ortalama kargo maliyeti hedefte mi?",_Y,_m,_I,[],"Lojistik API + finans tablosu gerekli.",False,9),
            ("E10","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Bu ay kargo firmalarının servis kalitesi değerlendirmesi?",_O,_m,_I,[],"Kargo API + CRM gerekli.",False,10),
            ("E11","E","Operasyon ve Lojistik","eticaret","eticaret","LojistikAgent (Yeni)","Aylık iade işleme maliyeti nedir? İadenin önüne geçmek için hangi ürünler öncelikli müdahale gerektirir?",_O,_m,_I,[],"İade API + finans tablosu gerekli.",False,11),
            # ── F. KATALOG KALİTESİ / PIM ────────────────────────────────────
            ("F01","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","PIM'de yayında (useInternet=True) olup e-ticaret sitesinde görünmeyen ürünler var mı? Senkronizasyon hatası var mı?",_K,_d,_S,["pim"],"PIM tarafı kontrol edilebilir, site tarafı site audit gerektirir.",False,1),
            ("F02","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Son 24 saatte PIM'de bloke edilen ürünler siteden gerçekten kalktı mı?",_K,_d,_S,["pim"],"",False,2),
            ("F03","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Yayında olup tüm renk/bedenleri bloke olan ürünler hangileri?",_Y,_d,_S,["pim"],"",False,3),
            ("F04","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Yayındaki ürünlerin fiyat güncellemesi e-ticaret sitesine yansıdı mı? Fiyat uyuşmazlığı olan ürün var mı?",_Y,_d,_P,["pim_fiyat"],"Gerçek site fiyatıyla karşılaştırma için site scrape/API gerekli.",False,4),
            ("F05","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Dün yeni yayına giren ürünlerin görsel sayısı yeterli mi? (Minimum 3 görsel kriteri) Görselsiz aktif ürün var mı?",_O,_d,_S,["pim"],"",False,5),
            ("F06","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Bu hafta enrichment yapılan ürün sayısı kaç? Grade A'ya yükselen ürünler hangileri?",_Y,_w,_S,["enrichment_quality"],"",False,6),
            ("F07","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Grade D ve F olan ürünlerin satış performansı nasıl? Grade artışının satışa etkisi ölçülebildi mi?",_Y,_w,_S,["enrichment_quality","mv_net_satis_urun"],"",False,7),
            ("F08","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Uzun süredir (14 gün+) sıralamada değişiklik yapılmamış kategoriler hangileri? Hangi ekip üyesi sorumlu?",_K,_w,_S,["siralama_gecmisi"],"",False,8),
            ("F09","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Kategori sıralamasında sürekli en altta kalan ve düşük satışlı ürünler hangileri? Sıralama skoru neden düşük?",_Y,_w,_S,["siralama_gecmisi","mv_net_satis_urun"],"",False,9),
            ("F10","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Ürün açıklaması boş veya çok kısa (100 karakterden az) olan yayındaki ürünler hangileri?",_Y,_w,_S,["pim","enrichment_quality"],"",False,10),
            ("F11","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Bu hafta görsel analizör tarafından önerilen güncelleme sayısı nedir? Kaç ürün onaylanarak PLM'e yazıldı?",_O,_w,_S,["vision_analysis_results"],"",False,11),
            ("F12","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Ürün kategorisi ataması yanlış olan ürünler var mı?",_O,_w,_P,["pim"],"Yanlışlık tespiti için referans kategori ağacı ve kural seti gerekli.",False,12),
            ("F13","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Aylık enrichment skoru ortalaması nedir? Grade dağılımı (A/B/C/D/F) geçen aya göre nasıl değişti?",_Y,_m,_S,["enrichment_season_summary","kiyaslama"],"",False,13),
            ("F14","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","SEO açısından ürün başlığı ve meta açıklaması eksik olan ürünler kaç adet?",_Y,_m,_P,["pim"],"Arama motorlarındaki görünürlük etkisi için Google Search Console gerekli.",False,14),
            ("F15","F","Katalog Kalitesi ve PIM","eticaret","urun_yonetimi","EnrichmentAgent + SiralamaAgent","Aylık olarak katalogdan çıkarılan (deaktif edilen) ürün sayısı nedir? Nedenler?",_O,_m,_S,["pim"],"",False,15),
            # ── G. EKİP YÖNETİMİ VE SÜREÇ ───────────────────────────────────
            ("G01","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Dün ekipten kim hangi kategori/görevi tamamladı? Bekleyen görevlerde gecikme var mı?",_K,_d,_I,[],"Görev yönetim sistemi (Jira/Asana) gerekli.",False,1),
            ("G02","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bugün ekip kapasitesi yeterli mi? İzinli/yokluk olan ekip üyesi var mı?",_Y,_d,_I,[],"İK sistemi / takvim gerekli.",False,2),
            ("G03","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Dün içerik ekibi kaç ürün için zenginleştirme tamamladı? Hedeflenen ürün adedine ulaşıldı mı?",_Y,_d,_S,["enrichment_quality"],"",False,3),
            ("G04","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Dün kategori yönetimi ekibi kaç kategoride sıralama güncelledi? Günlük hedef tutturuldu mu?",_Y,_d,_S,["siralama_gecmisi"],"",False,4),
            ("G05","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bekleyen acil ürün onayları var mı? (Yeni ürün yayınlama, fiyat onayı, kampanya onayı)",_O,_d,_I,[],"Onay akışı (workflow) sistemi gerekli.",False,5),
            ("G06","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bu hafta ekip üyesi bazında üretkenlik raporu: Kaç ürün enrichment, kaç kategori güncelleme?",_K,_w,_S,["enrichment_quality","siralama_gecmisi"],"İçerik üretimi için ayrı görev takip sistemi gerekebilir.",False,6),
            ("G07","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Uzun süredir atanmış ama tamamlanmamış görevler hangileri?",_K,_w,_I,[],"Görev yönetim sistemi gerekli.",False,7),
            ("G08","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bu hafta en uzun süre işlem görmemiş ürün/kategori hangileri? Kim sorumlu?",_Y,_w,_S,["pim","siralama_gecmisi"],"",False,8),
            ("G09","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Kalite kontrol sürecindeki bekleyen ürün sayısı nedir? QC turnaround süresi hedefte mi?",_Y,_w,_I,[],"Onay akışı sistemi gerekli.",False,9),
            ("G10","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bu hafta ekip içi hata oranı nedir? (Yanlış fiyat girişi, hatalı kategori, yanlış görsel atama)",_O,_w,_I,[],"Hata logu gerekli.",False,10),
            ("G11","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Ajans/dış ekip teslimatları bu hafta zamanında geldi mi?",_Y,_w,_I,[],"Proje yönetim sistemi gerekli.",False,11),
            ("G12","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bu hafta ekip eğitim/gelişim aktivitesi var mı?",_O,_w,_I,[],"İK / öğrenme platformu gerekli.",False,12),
            ("G13","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Aylık ekip performans değerlendirmesi: KPI'lar tutturuldu mu? Ekip üyesi bazında değerlendirme?",_K,_m,_I,[],"KPI takip sistemi gerekli.",False,13),
            ("G14","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bu ay en çok geri dönüş gerektiren hatalı iş hangileri?",_Y,_m,_I,[],"Hata logu + proje yönetim sistemi gerekli.",False,14),
            ("G15","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Aylık ekip doluluk oranı (capacity utilization) nedir?",_O,_m,_I,[],"Görev yönetim sistemi gerekli.",False,15),
            ("G16","G","Ekip Yönetimi ve Süreç Kontrolü","eticaret","kiyaslama","SüreçAgent (Yeni)","Bu ay işe alınan veya ekipten ayrılan biri var mı? Ekip kapasitesine etkisi nedir?",_Y,_m,_I,[],"İK sistemi gerekli.",False,16),
            # ── H. REKABET VE PAZAR ───────────────────────────────────────────
            ("H01","H","Rekabet ve Pazar","eticaret","kiyaslama","RekabetAgent (Yeni)","Bu hafta rakip markalar hangi yeni ürünleri/koleksiyonları yayınladı?",_Y,_w,_I,[],"Rekabet takip aracı / web scraping gerekli.",False,1),
            ("H02","H","Rekabet ve Pazar","eticaret","kiyaslama","RekabetAgent (Yeni)","Rakiplerin bu haftaki kampanya ve indirim stratejisi nedir? Fiyat savaşı riski var mı?",_Y,_w,_I,[],"Rekabet takip aracı gerekli.",False,2),
            ("H03","H","Rekabet ve Pazar","eticaret","kiyaslama","RekabetAgent (Yeni)","Pazar yeri (Trendyol, HB vb.) sıralamalarımız bu hafta nasıl değişti?",_O,_w,_I,[],"Pazar yeri analytics gerekli.",False,3),
            ("H04","H","Rekabet ve Pazar","eticaret","kiyaslama","RekabetAgent (Yeni)","Sosyal medyada marka bahsedilme sayısı ve tonu bu hafta nasıl?",_O,_w,_I,[],"Sosyal dinleme aracı gerekli.",False,4),
            ("H05","H","Rekabet ve Pazar","eticaret","kiyaslama","RekabetAgent (Yeni)","Aylık pazar payı tahmini nasıl? Hangi kategoride pazar payı kaybediyoruz?",_Y,_m,_I,[],"Sektör raporları + satış API gerekli.",False,5),
            ("H06","H","Rekabet ve Pazar","eticaret","kiyaslama","RekabetAgent (Yeni)","Bu ay rakiplerin hangi ürün/koleksiyonu en fazla ilgi gördü?",_Y,_m,_I,[],"Rekabet takip aracı gerekli.",False,6),
            ("H07","H","Rekabet ve Pazar","eticaret","kiyaslama","RekabetAgent (Yeni)","Fiyat konumlandırmamız rakiplere göre nasıl? Hangi kategoride rekabetçi fiyat avantajımız var?",_O,_m,_I,[],"Fiyat karşılaştırma aracı gerekli.",False,7),
            # ── I. TEKNOLOJİ VE SİTE SAĞLIĞI ─────────────────────────────────
            ("I01","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Site hızı (Core Web Vitals) dün nasıldı? Mobil ve masaüstü yüklenme süreleri hedefte mi?",_K,_d,_I,[],"Google Search Console / Lighthouse API gerekli.",False,1),
            ("I02","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Dün 404, 500 gibi hata oranları normalin üzerinde mi?",_K,_d,_I,[],"Server logu / site monitoring gerekli.",False,2),
            ("I03","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Ödeme sistemi stabilitesi: Dün ödeme hatası oranı nedir?",_Y,_d,_I,[],"Payment gateway logu gerekli.",False,3),
            ("I04","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Mobil vs masaüstü satış oranı dün nasıl? Mobil dönüşümde anormal bir düşüş var mı?",_O,_d,_P,["incorta_analytics"],"Cihaz bazlı kırılım sınırlı olabilir.",False,4),
            ("I05","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Bu hafta site arama motorunun kalitesi nasıl?",_Y,_w,_I,[],"Site arama logu gerekli.",False,5),
            ("I06","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","A/B test sonuçları: Bu hafta hangi test sonuçlandı?",_Y,_w,_I,[],"A/B test platformu gerekli.",False,6),
            ("I07","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Bu hafta sitede teknik hata bildirilen kullanıcı şikayeti sayısı?",_O,_w,_I,[],"CRM + site monitoring gerekli.",False,7),
            ("I08","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Site içi öneri motoru (recommendation engine) bu hafta ne kadar ek satış yarattı?",_O,_w,_I,[],"Analytics + satış API gerekli.",False,8),
            ("I09","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Aylık site uptime oranı nedir? Downtime yaşandıysa tahmini ciro kaybı nedir?",_Y,_m,_I,[],"Monitoring sistemi gerekli.",False,9),
            ("I10","I","Teknoloji ve Site Sağlığı","eticaret","eticaret","TeknolojiAgent (Yeni)","Bu ay yapılan teknik güncellemeler satış/dönüşüm üzerinde olumlu etki yarattı mı?",_O,_m,_I,[],"Analytics (öncesi/sonrası karşılaştırma) gerekli.",False,10),
            # ── J. STRATEJİK VE FİNANSAL ──────────────────────────────────────
            ("J01","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Bu haftanın 3 en önemli önceliği nedir? Geçen haftadan devam eden kritik konu var mı?",_K,_w,_P,[],"Sadece satış + katalog + enrichment boyutunda sentez yapılabilir. Tam kapsam için tüm alanların verisi gerekli.",True,1),
            ("J02","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Bu hafta alınması gereken acil karar var mı? (Kampanya değişikliği, bütçe transferi, acil stok kararı)",_Y,_w,_P,[],"Çapraz analiz (kısmi).",True,2),
            ("J03","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Mevcut trend devam ederse bu ay kapanışta hedeften sapma ne kadar olur? (Erken uyarı)",_Y,_w,_P,["incorta_satis","hedef_tablosu"],"Basit lineer projeksiyon yapılabilir. Gelişmiş tahmin modeli henüz yok.",False,3),
            ("J04","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Bu hafta ekip için blocker (engel) nedir? Ne çözülürse en fazla büyüme sağlanır?",_O,_w,_I,[],"Ekip feedback + görev takip sistemi gerekli.",True,4),
            ("J05","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Geçen ayın özet değerlendirmesi: Ne iyi gitti, ne kötü gitti, bu ay ne farklı yapmalıyız?",_K,_m,_P,[],"Çapraz analiz (mevcut agent'ların sentezi, kısmi kapsam).",True,5),
            ("J06","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Aylık e-ticaret kârlılık analizi: Brüt marj, pazarlama maliyeti, lojistik maliyeti düşüldükten sonra net katkı marjı nedir?",_K,_m,_P,["mv_net_satis_aylik"],"Pazarlama ve lojistik maliyet verileri yok. Sadece brüt-net ciro karşılaştırması yapılabilir.",False,6),
            ("J07","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Gelecek ay için büyüme planı: Hedef, strateji ve bütçe hazır mı?",_Y,_m,_I,[],"Planlama dokümanı gerekli.",False,7),
            ("J08","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Sezon sonuna kadar stok tükenme projeksiyonu: Hangi kategoride erken stok tükenmesi riski var?",_Y,_m,_S,["pimland_mcp_stok","incorta_satis"],"",False,8),
            ("J09","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Bu ay hangi teknoloji yatırımı en fazla ROI sağladı?",_O,_m,_I,[],"Teknoloji gider tablosu + Analytics gerekli.",False,9),
            ("J10","J","Stratejik ve Finansal","eticaret","kiyaslama","Orchestrator (Çapraz)","Ekip büyütme veya yeniden yapılanma ihtiyacı var mı?",_O,_m,_I,[],"Kapasite analizi + büyüme planı gerekli.",False,10),
        ]
        try:
            async with engine.begin() as _sc:
                cnt = (await _sc.execute(_text("SELECT COUNT(*) FROM brief_question_library"))).scalar()
                if cnt and cnt >= 122:
                    return
                # Reseed: eski veriler temizlenir
                await _sc.execute(_text("TRUNCATE brief_question_library RESTART IDENTITY"))
                for q in _QUESTIONS:
                    (code, cat_code, category, dept, agent, agent_label,
                     text, imp, freq, status, sources, constraint, cross, sort) = q
                    await _sc.execute(_text("""
                        INSERT INTO brief_question_library
                          (question_code, category_code, category, department,
                           agent, agent_label, question_text, importance, frequency,
                           data_status, data_sources, constraints_note, is_cross_domain, sort_order)
                        VALUES
                          (:code, :cat_code, :category, :dept,
                           :agent, :agent_label, :text, :imp, :freq,
                           :status, CAST(:sources AS JSONB), :constraint, :cross, :sort)
                    """), {
                        "code": code, "cat_code": cat_code, "category": category,
                        "dept": dept, "agent": agent, "agent_label": agent_label,
                        "text": text, "imp": imp, "freq": freq, "status": status,
                        "sources": _json.dumps(sources, ensure_ascii=False),
                        "constraint": constraint or None, "cross": cross, "sort": sort,
                    })
                log.info("brief_question_library.seeded", count=len(_QUESTIONS))
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
