-- Pimland AI Reporting — Index'ler
-- Öncelik 1: Sorgu optimizasyonu
-- Opus analizi 2026-06-03

-- Trigram extension (keyword arama için)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── incorta_satis ─────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_satis_yil_ay
  ON incorta_satis (yil, ay);
CREATE INDEX IF NOT EXISTS idx_satis_kanal
  ON incorta_satis (satis_kanali, yil, ay);
CREATE INDEX IF NOT EXISTS idx_satis_sku
  ON incorta_satis (urun_kodu);

-- ── incorta_depo_iade ─────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_iade_yil_ay
  ON incorta_depo_iade (yil, ay);
CREATE INDEX IF NOT EXISTS idx_iade_sku
  ON incorta_depo_iade (urun_kodu);

-- ── incorta_iptal_siparis ─────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_iptal_yil_ay
  ON incorta_iptal_siparis (yil, ay);
CREATE INDEX IF NOT EXISTS idx_iptal_sku
  ON incorta_iptal_siparis (urun_kodu);

-- ── incorta_analytics ────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_analytics_date
  ON incorta_analytics (date);
CREATE INDEX IF NOT EXISTS idx_analytics_marka
  ON incorta_analytics (marka, date);

-- ── pim_products ──────────────────────────────────────────────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku
  ON pim_products (urun_kodu);
CREATE INDEX IF NOT EXISTS idx_products_brand_season
  ON pim_products (marka_adi, sezon_kodu);
CREATE INDEX IF NOT EXISTS idx_products_name_trgm
  ON pim_products USING gin (urun_adi gin_trgm_ops);
