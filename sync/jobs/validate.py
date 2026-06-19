"""Sync sonrası veri bütünlüğü kontrolleri.

Kontroller:
  1. Orphan SKU — incorta'da olup pim_products'ta olmayan ürünler
  2. Boş tablo — her tabloda en az 1 kayıt
  3. Net ciro sağlık — bu haftaki ciro geçen haftanın %50'sinden az mı
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import psycopg2

log = logging.getLogger(__name__)

_TABLOLAR = [
    "incorta_satis",
    "incorta_depo_iade",
    "incorta_iptal_siparis",
    "incorta_analytics",
    "incorta_ecommerce_gunluk",
    "incorta_magaza_performans",
    "pim_products",
]

_ORPHAN_ESIK = 500      # Bu kadarın üzerinde orphan → kritik
_CIRO_ESIK   = 0.50     # Net ciro geçen haftanın %50'sinden azsa uyarı


def _check_orphan_sku(cur: "psycopg2.cursor") -> Dict[str, Any]:
    cur.execute("""
        SELECT COUNT(DISTINCT s.urun_kodu) AS orphan_count
        FROM incorta_satis s
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        WHERE p.urun_kodu IS NULL
    """)
    count = cur.fetchone()[0]
    seviye = "kritik" if count > _ORPHAN_ESIK else ("uyari" if count > 0 else "ok")
    return {
        "kontrol": "orphan_sku",
        "deger":   count,
        "seviye":  seviye,
        "mesaj":   f"incorta_satis'te pim_products'ta bulunmayan {count} SKU" if count else "Orphan SKU yok",
    }


def _check_empty_tables(cur: "psycopg2.cursor") -> List[Dict[str, Any]]:
    results = []
    for tablo in _TABLOLAR:
        cur.execute(f"SELECT COUNT(*) FROM {tablo}")
        count = cur.fetchone()[0]
        seviye = "kritik" if count == 0 else "ok"
        results.append({
            "kontrol": f"bos_tablo_{tablo}",
            "deger":   count,
            "seviye":  seviye,
            "mesaj":   f"{tablo} BOŞ!" if count == 0 else f"{tablo}: {count:,} kayıt",
        })
    return results


def _check_net_ciro_health(cur: "psycopg2.cursor") -> Dict[str, Any]:
    """mv_net_satis_kanal üzerinden bu ay vs geçen ay toplam ciro karşılaştırması."""
    cur.execute("""
        SELECT COUNT(*) FROM pg_matviews
        WHERE matviewname='mv_net_satis_aylik' AND schemaname='public'
    """)
    if cur.fetchone()[0] == 0:
        return {"kontrol": "net_ciro_saglik", "seviye": "skip", "mesaj": "mv_net_satis_aylik yok"}

    cur.execute("""
        SELECT
            SUM(CASE WHEN (yil * 100 + ay) = (
                SELECT MAX(yil * 100 + ay) FROM mv_net_satis_aylik
            ) THEN net_ciro ELSE 0 END) AS bu_ay,
            SUM(CASE WHEN (yil * 100 + ay) = (
                SELECT MAX(yil * 100 + ay) - 1
                FROM mv_net_satis_aylik
                WHERE ay > 1  -- ay=1 ise önceki ay aralık (yil-1,12)
            ) THEN net_ciro ELSE 0 END) AS gecen_ay
        FROM mv_net_satis_aylik
    """)
    row = cur.fetchone()
    bu_ay, gecen_ay = (row[0] or 0), (row[1] or 0)

    if gecen_ay == 0:
        return {"kontrol": "net_ciro_saglik", "seviye": "skip", "mesaj": "Geçen ay verisi yok"}

    oran = bu_ay / gecen_ay
    seviye = "uyari" if oran < _CIRO_ESIK else "ok"
    return {
        "kontrol": "net_ciro_saglik",
        "deger":   round(oran, 2),
        "seviye":  seviye,
        "mesaj":   (
            f"Bu ay cirosu geçen ayın {oran:.0%}'i — DİKKAT!"
            if seviye == "uyari"
            else f"Net ciro sağlıklı ({oran:.0%})"
        ),
    }


def run_validation(db_conn: "psycopg2.connection") -> Dict[str, Any]:
    """Tüm kontrolleri çalıştır, uyarı ve kritikleri döndür."""
    uyarilar: List[Dict] = []
    kritikler: List[Dict] = []

    with db_conn.cursor() as cur:
        sonuclar = []

        # 1. Orphan SKU
        try:
            r = _check_orphan_sku(cur)
            sonuclar.append(r)
        except Exception as e:
            log.error("orphan kontrol hatası: %s", e)

        # 2. Boş tablo kontrolü
        try:
            for r in _check_empty_tables(cur):
                sonuclar.append(r)
        except Exception as e:
            log.error("bos tablo kontrol hatası: %s", e)

        # 3. Net ciro sağlık
        try:
            r = _check_net_ciro_health(cur)
            sonuclar.append(r)
        except Exception as e:
            log.error("ciro saglik kontrol hatası: %s", e)

    for s in sonuclar:
        seviye = s.get("seviye", "ok")
        if seviye == "kritik":
            kritikler.append(s)
            log.error("[VALİDASYON KRİTİK] %s: %s", s["kontrol"], s["mesaj"])
        elif seviye == "uyari":
            uyarilar.append(s)
            log.warning("[VALİDASYON UYARI] %s: %s", s["kontrol"], s["mesaj"])
        else:
            log.info("[VALİDASYON OK] %s: %s", s["kontrol"], s.get("mesaj", ""))

    return {
        "uyarilar": uyarilar,
        "kritik":   kritikler,
        "toplam_kontrol": len(sonuclar),
        "ozet": f"{len(kritikler)} kritik, {len(uyarilar)} uyarı, {len(sonuclar)-len(kritikler)-len(uyarilar)} OK",
    }
