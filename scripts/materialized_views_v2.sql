-- Pimland AI Reporting — Materialized Views v2
-- Incorta tabanlı — CTE ile önce aggregate, sonra join (performans kritik)
-- 2026-06-03

-- ── View 1: mv_net_satis_aylik ───────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_net_satis_aylik AS
WITH s AS (
  SELECT yil, ay, satis_kanali,
    SUM(tutar) AS brut_ciro, SUM(adet) AS brut_adet
  FROM incorta_satis GROUP BY yil, ay, satis_kanali
),
i AS (
  SELECT yil, ay, satis_kanali,
    SUM(tutar) AS iade_ciro, SUM(adet) AS iade_adet
  FROM incorta_depo_iade GROUP BY yil, ay, satis_kanali
),
ip AS (
  SELECT yil, ay, satis_kanali,
    SUM(tutar) AS iptal_ciro, SUM(adet) AS iptal_adet
  FROM incorta_iptal_siparis GROUP BY yil, ay, satis_kanali
)
SELECT
  s.yil, s.ay, s.satis_kanali,
  s.brut_ciro,
  COALESCE(i.iade_ciro, 0)                                  AS iade_ciro,
  COALESCE(ip.iptal_ciro, 0)                                AS iptal_ciro,
  s.brut_ciro + COALESCE(i.iade_ciro,0) + COALESCE(ip.iptal_ciro,0) AS net_ciro,
  s.brut_adet,
  s.brut_adet + COALESCE(i.iade_adet,0) + COALESCE(ip.iptal_adet,0) AS net_adet,
  ROUND((ABS(COALESCE(i.iade_ciro,0)) / NULLIF(s.brut_ciro,0) * 100)::numeric, 1) AS iade_oran_pct
FROM s
LEFT JOIN i  USING (yil, ay, satis_kanali)
LEFT JOIN ip USING (yil, ay, satis_kanali)
ORDER BY s.yil, s.ay;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_net_satis_aylik
  ON mv_net_satis_aylik (yil, ay, satis_kanali);

-- ── View 2: mv_net_satis_urun ────────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_net_satis_urun AS
WITH s AS (
  SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
    SUM(tutar) AS brut_ciro, SUM(adet) AS brut_adet,
    COUNT(DISTINCT satis_kanali) AS kanal_sayisi
  FROM incorta_satis GROUP BY urun_kodu
),
i AS (
  SELECT urun_kodu, SUM(tutar) AS iade_ciro, SUM(adet) AS iade_adet
  FROM incorta_depo_iade GROUP BY urun_kodu
),
ip AS (
  SELECT urun_kodu, SUM(tutar) AS iptal_ciro, SUM(adet) AS iptal_adet
  FROM incorta_iptal_siparis GROUP BY urun_kodu
)
SELECT
  s.urun_kodu, s.urun_adi,
  s.brut_ciro,
  COALESCE(i.iade_ciro, 0)                                  AS iade_ciro,
  COALESCE(ip.iptal_ciro, 0)                                AS iptal_ciro,
  s.brut_ciro + COALESCE(i.iade_ciro,0) + COALESCE(ip.iptal_ciro,0) AS net_ciro,
  s.brut_adet,
  s.brut_adet + COALESCE(i.iade_adet,0) + COALESCE(ip.iptal_adet,0) AS net_adet,
  ROUND((ABS(COALESCE(i.iade_ciro,0)) / NULLIF(s.brut_ciro,0) * 100)::numeric, 1) AS iade_oran_pct,
  s.kanal_sayisi
FROM s
LEFT JOIN i  USING (urun_kodu)
LEFT JOIN ip USING (urun_kodu)
ORDER BY net_ciro DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_net_satis_urun
  ON mv_net_satis_urun (urun_kodu);

-- ── View 3: mv_net_satis_kanal ───────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_net_satis_kanal AS
WITH s AS (
  SELECT satis_kanali,
    SUM(tutar) AS brut_ciro, SUM(adet) AS brut_adet
  FROM incorta_satis GROUP BY satis_kanali
),
i AS (
  SELECT satis_kanali, SUM(tutar) AS iade_ciro, SUM(adet) AS iade_adet
  FROM incorta_depo_iade GROUP BY satis_kanali
),
ip AS (
  SELECT satis_kanali, SUM(tutar) AS iptal_ciro, SUM(adet) AS iptal_adet
  FROM incorta_iptal_siparis GROUP BY satis_kanali
),
net AS (
  SELECT
    s.satis_kanali, s.brut_ciro, s.brut_adet,
    COALESCE(i.iade_ciro, 0)  AS iade_ciro,
    COALESCE(ip.iptal_ciro,0) AS iptal_ciro,
    COALESCE(i.iade_adet, 0)  AS iade_adet,
    COALESCE(ip.iptal_adet,0) AS iptal_adet
  FROM s LEFT JOIN i USING (satis_kanali) LEFT JOIN ip USING (satis_kanali)
)
SELECT
  satis_kanali,
  brut_ciro,
  iade_ciro,
  iptal_ciro,
  brut_ciro + iade_ciro + iptal_ciro                        AS net_ciro,
  brut_adet,
  brut_adet + iade_adet + iptal_adet                        AS net_adet,
  ROUND((brut_ciro * 100.0 / NULLIF(SUM(brut_ciro) OVER (), 0))::numeric, 1) AS pazar_payi_pct,
  ROUND((ABS(iade_ciro) / NULLIF(brut_ciro,0) * 100)::numeric, 1)            AS iade_oran_pct
FROM net
ORDER BY net_ciro DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_net_satis_kanal
  ON mv_net_satis_kanal (satis_kanali);

-- ── View 4: mv_satis_marka_sezon ─────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_satis_marka_sezon AS
WITH s AS (
  SELECT urun_kodu,
    SUM(tutar) AS brut_ciro, SUM(adet) AS brut_adet
  FROM incorta_satis GROUP BY urun_kodu
),
i AS (
  SELECT urun_kodu, SUM(tutar) AS iade_ciro, SUM(adet) AS iade_adet
  FROM incorta_depo_iade GROUP BY urun_kodu
),
ip AS (
  SELECT urun_kodu, SUM(tutar) AS iptal_ciro, SUM(adet) AS iptal_adet
  FROM incorta_iptal_siparis GROUP BY urun_kodu
)
SELECT
  COALESCE(p.marka_adi,   'Bilinmiyor') AS marka_adi,
  COALESCE(p.sezon_kodu,  'Bilinmiyor') AS sezon_kodu,
  COALESCE(p.sezon_adi,   'Bilinmiyor') AS sezon_adi,
  SUM(s.brut_ciro)                      AS brut_ciro,
  SUM(s.brut_ciro + COALESCE(i.iade_ciro,0) + COALESCE(ip.iptal_ciro,0)) AS net_ciro,
  SUM(s.brut_adet)                      AS brut_adet,
  SUM(s.brut_adet + COALESCE(i.iade_adet,0) + COALESCE(ip.iptal_adet,0)) AS net_adet,
  COUNT(DISTINCT s.urun_kodu)           AS sku_sayisi
FROM s
LEFT JOIN pim_products p  ON p.urun_kodu = s.urun_kodu
LEFT JOIN i               ON s.urun_kodu = i.urun_kodu
LEFT JOIN ip              ON s.urun_kodu = ip.urun_kodu
GROUP BY p.marka_adi, p.sezon_kodu, p.sezon_adi
ORDER BY net_ciro DESC;

CREATE INDEX IF NOT EXISTS idx_mv_satis_marka_sezon
  ON mv_satis_marka_sezon (marka_adi, sezon_kodu);

-- ── View 5: mv_satis_kategori ────────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_satis_kategori AS
WITH s AS (
  SELECT urun_kodu,
    SUM(tutar) AS brut_ciro, SUM(adet) AS brut_adet
  FROM incorta_satis GROUP BY urun_kodu
),
i AS (
  SELECT urun_kodu, SUM(tutar) AS iade_ciro, SUM(adet) AS iade_adet
  FROM incorta_depo_iade GROUP BY urun_kodu
),
ip AS (
  SELECT urun_kodu, SUM(tutar) AS iptal_ciro, SUM(adet) AS iptal_adet
  FROM incorta_iptal_siparis GROUP BY urun_kodu
)
SELECT
  COALESCE(p.ana_grup_adi,   'Bilinmiyor') AS ana_grup_adi,
  COALESCE(p.urun_grubu_adi, 'Bilinmiyor') AS urun_grubu_adi,
  SUM(s.brut_ciro + COALESCE(i.iade_ciro,0) + COALESCE(ip.iptal_ciro,0)) AS net_ciro,
  SUM(s.brut_adet + COALESCE(i.iade_adet,0) + COALESCE(ip.iptal_adet,0)) AS net_adet,
  ROUND((ABS(SUM(COALESCE(i.iade_ciro,0))) / NULLIF(SUM(s.brut_ciro),0) * 100)::numeric, 1) AS iade_oran_pct,
  COUNT(DISTINCT s.urun_kodu) AS sku_sayisi
FROM s
LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
LEFT JOIN i              ON s.urun_kodu = i.urun_kodu
LEFT JOIN ip             ON s.urun_kodu = ip.urun_kodu
GROUP BY p.ana_grup_adi, p.urun_grubu_adi
ORDER BY net_ciro DESC;

CREATE INDEX IF NOT EXISTS idx_mv_satis_kategori
  ON mv_satis_kategori (ana_grup_adi, urun_grubu_adi);

-- ── View 6: mv_analytics_kanal ───────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_analytics_kanal AS
SELECT
  marka, oturum_kaynagi,
  SUM(kullanicilar)                                AS toplam_kullanici,
  SUM(oturumlar)                                   AS toplam_oturum,
  SUM(ciro)                                        AS toplam_ciro,
  SUM(islem_sayisi)                                AS toplam_islem,
  ROUND((AVG(conversion_rate) * 100)::numeric, 2)  AS ort_conversion_pct,
  ROUND((AVG(hemen_cikma_orani) * 100)::numeric, 2) AS ort_bounce_pct
FROM incorta_analytics
GROUP BY marka, oturum_kaynagi
ORDER BY toplam_ciro DESC;

CREATE INDEX IF NOT EXISTS idx_mv_analytics_kanal
  ON mv_analytics_kanal (marka, oturum_kaynagi);

-- ── View 7: mv_analytics_gunluk ──────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_analytics_gunluk AS
SELECT
  date                                             AS tarih,
  marka,
  SUM(oturumlar)                                   AS toplam_oturum,
  SUM(ciro)                                        AS toplam_ciro,
  SUM(islem_sayisi)                                AS toplam_islem,
  ROUND((AVG(conversion_rate) * 100)::numeric, 2)  AS ort_conversion_pct
FROM incorta_analytics
GROUP BY date, marka
ORDER BY date DESC, marka;

CREATE INDEX IF NOT EXISTS idx_mv_analytics_gunluk
  ON mv_analytics_gunluk (tarih, marka);
