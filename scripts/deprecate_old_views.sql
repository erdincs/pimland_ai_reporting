-- Pimland AI Reporting — Eski materialized view'ları kaldır
-- Excel (eticaret_satis) tabanlı — mv_net_satis_* ile değiştirildi
-- 2026-06-03
--
-- NOT: Bu view'lar postgres superuser tarafından oluşturuldu.
-- Çalıştırmak için: sudo -u postgres psql pimland_reporting -f this_file.sql
-- Veya pgAdmin'den superuser ile bağlanarak çalıştırın.
-- Yeni view'lar (mv_net_satis_*) zaten aktif — bu drop ertelenebilir.

DROP MATERIALIZED VIEW IF EXISTS mv_satis_aylik;
DROP MATERIALIZED VIEW IF EXISTS mv_satis_urun;
DROP MATERIALIZED VIEW IF EXISTS mv_satis_kanal;
