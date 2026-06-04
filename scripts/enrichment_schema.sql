-- =============================================
-- PRODUCT ENRICHMENT — Kalite Puanlama Şeması
-- Mevcut tablolara dokunulmaz
-- =============================================

CREATE TABLE IF NOT EXISTS enrichment_quality (
  id SERIAL PRIMARY KEY,
  urun_kodu VARCHAR(50) NOT NULL,
  sezon_kodu VARCHAR(20),
  sezon_adi VARCHAR(100),

  -- Genel puan (0-100)
  quality_score INTEGER DEFAULT 0,
  quality_grade VARCHAR(1),  -- A/B/C/D/F

  -- Kategori puanları (her biri 0-25)
  score_temel_bilgi INTEGER DEFAULT 0,
  score_kumas_bilgi INTEGER DEFAULT 0,
  score_gorsel INTEGER DEFAULT 0,
  score_satis_icerik INTEGER DEFAULT 0,

  -- Sorunlar
  eksik_alanlar JSONB DEFAULT '[]',
  hatali_alanlar JSONB DEFAULT '[]',
  uyarilar JSONB DEFAULT '[]',

  -- Meta
  last_scored_at TIMESTAMP DEFAULT NOW(),
  pimland_updated_at TIMESTAMP,

  UNIQUE(urun_kodu)
);

CREATE TABLE IF NOT EXISTS enrichment_season_summary (
  id SERIAL PRIMARY KEY,
  sezon_kodu VARCHAR(20) NOT NULL UNIQUE,
  sezon_adi VARCHAR(100),
  toplam_urun INTEGER DEFAULT 0,
  ortalama_puan NUMERIC(5,1),
  grade_a INTEGER DEFAULT 0,
  grade_b INTEGER DEFAULT 0,
  grade_c INTEGER DEFAULT 0,
  grade_d INTEGER DEFAULT 0,
  grade_f INTEGER DEFAULT 0,
  top_sorunlar JSONB DEFAULT '[]',
  scored_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eq_sezon  ON enrichment_quality (sezon_kodu);
CREATE INDEX IF NOT EXISTS idx_eq_score  ON enrichment_quality (quality_score);
CREATE INDEX IF NOT EXISTS idx_eq_grade  ON enrichment_quality (quality_grade);
CREATE INDEX IF NOT EXISTS idx_eq_urun   ON enrichment_quality (urun_kodu);
