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


def _get_view_order() -> list:
    """views.yaml'dan view listesini al; yüklenemezse hardcode fallback."""
    try:
        from sync.config_loader import load_views
        return [v["name"] for v in load_views()]
    except Exception as e:
        log.warning("views.yaml yüklenemedi, fallback kullanılıyor: %s", e)
        return [
            "mv_net_satis_aylik", "mv_net_satis_urun", "mv_net_satis_kanal",
            "mv_analytics_kanal", "mv_analytics_gunluk",
            "mv_satis_marka_sezon", "mv_satis_kategori",
        ]


def refresh_all_views(db_conn: "psycopg2.connection") -> Dict[str, Any]:
    """Tüm view'ları sırayla refresh et, hataları topla."""
    basarili: List[Dict] = []
    hatali:   List[Dict] = []

    for view in _get_view_order():
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
