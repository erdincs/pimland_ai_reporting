-- ─── Pimland E-Ticaret Brüt Satış — Pre-aggregation Views ──────────────────
-- Kaynak tablo: eticaret_satis  (95.393 satır, 2026 YTD)
-- Agent schema_context'te bu view'lara yönlendirilir; ham tablodan çok daha hızlı.
-- Her Excel yüklemesinden sonra aşağıdaki REFRESH komutlarını çalıştır.

-- ── 1. Aylık kanal bazlı özet ───────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_satis_aylik AS
SELECT
    yil,
    ay,
    satiskanali,
    SUM(ciro)  AS toplam_ciro,
    SUM(adet)  AS toplam_adet
FROM eticaret_satis
GROUP BY yil, ay, satiskanali
WITH NO DATA;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_satis_aylik
    ON mv_satis_aylik (yil, ay, satiskanali);


-- ── 2. Ürün bazlı özet ──────────────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_satis_urun AS
SELECT
    itemcode,
    item,
    SUM(ciro)                     AS toplam_ciro,
    SUM(adet)                     AS toplam_adet,
    COUNT(DISTINCT satiskanali)   AS kanal_sayisi
FROM eticaret_satis
GROUP BY itemcode, item
WITH NO DATA;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_satis_urun
    ON mv_satis_urun (itemcode);


-- ── 3. Kanal bazlı pazar payı ────────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_satis_kanal AS
SELECT
    satiskanali,
    SUM(ciro)                                                         AS toplam_ciro,
    SUM(adet)                                                         AS toplam_adet,
    ROUND(
        (100.0 * SUM(ciro) / SUM(SUM(ciro)) OVER ())::numeric, 2
    )                                                                 AS pazar_payi
FROM eticaret_satis
GROUP BY satiskanali
WITH NO DATA;

-- Kanal view'u küçük olduğu için unique index yeterli.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_satis_kanal
    ON mv_satis_kanal (satiskanali);


-- ── İlk populate (ingest sonrası) ───────────────────────────────────────────
-- REFRESH MATERIALIZED VIEW mv_satis_aylik;
-- REFRESH MATERIALIZED VIEW mv_satis_urun;
-- REFRESH MATERIALIZED VIEW mv_satis_kanal;

-- ── Sonraki yüklemelerde (concurrent = okuma kilitlenmez) ───────────────────
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_satis_aylik;
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_satis_urun;
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_satis_kanal;
