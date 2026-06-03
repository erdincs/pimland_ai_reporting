"""Materialized view'ları bağımlılık sırasına göre refresh eder.

CONCURRENTLY kullanır — okuma sırasında engelleme olmaz.
Bir view başarısız olsa dahi diğerleri devam eder.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import psycopg2

log = logging.getLogger(__name__)

# Bağımlılık sırası: fact tabloları önce, pim_products JOIN'li view'lar sonra
VIEW_ORDER = [
    "mv_net_satis_aylik",     # fact tabloları (bağımsız)
    "mv_net_satis_urun",      # fact tabloları (bağımsız)
    "mv_net_satis_kanal",     # fact tabloları (bağımsız)
    "mv_analytics_kanal",     # incorta_analytics (bağımsız)
    "mv_analytics_gunluk",    # incorta_analytics (bağımsız)
    "mv_satis_marka_sezon",   # pim_products JOIN — ürün sync sonrası
    "mv_satis_kategori",      # pim_products JOIN — ürün sync sonrası
]


def refresh_all_views(db_conn: "psycopg2.connection") -> Dict[str, Any]:
    """Tüm view'ları sırayla refresh et, hataları topla."""
    basarili: List[Dict] = []
    hatali:   List[Dict] = []

    for view in VIEW_ORDER:
        t0 = time.perf_counter()
        log.info("view refresh başlıyor: %s", view)
        try:
            with db_conn.cursor() as cur:
                # View var mı kontrol et
                cur.execute(
                    "SELECT COUNT(*) FROM pg_matviews WHERE matviewname=%s AND schemaname='public'",
                    (view,)
                )
                if cur.fetchone()[0] == 0:
                    log.warning("view bulunamadı, atlanıyor: %s", view)
                    continue

                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
            db_conn.commit()

            sure = round(time.perf_counter() - t0, 1)
            log.info("view refresh bitti: %s sure=%.1fs", view, sure)
            basarili.append({"view": view, "sure_sn": sure})

        except Exception as exc:
            db_conn.rollback()
            sure = round(time.perf_counter() - t0, 1)
            log.error("view refresh hata: %s hata=%s", view, exc)
            hatali.append({"view": view, "hata": str(exc), "sure_sn": sure})

    toplam_sure = sum(v["sure_sn"] for v in basarili + hatali)
    log.info(
        "refresh_all_views bitti basarili=%d hatali=%d sure=%.1fs",
        len(basarili), len(hatali), toplam_sure,
    )
    return {
        "basarili": basarili,
        "hatali":   hatali,
        "toplam_sure_sn": round(toplam_sure, 1),
    }
