"""adL Premium Brief v2 · Günlük üretim handler'ı.

İki mod:
  AWS Lambda  → handler(event, context) entry point
  APScheduler → run_brief_daily() async fonksiyonu

Her gün 03:00 UTC (06:00 İstanbul) çalışır.
EC + MG brief'lerini üretip brief_history'ye yazar.
Hata → status='error', mesaj kaydedilir, istisna fırlatılmaz.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)

# Lambda soğuk başlatmada ağır import'lar geciktirilir
_INIT_DONE = False


def _lazy_init() -> None:
    global _INIT_DONE
    if _INIT_DONE:
        return
    # PYTHONPATH ayarı — Lambda'da /var/task kök dizin olur
    import sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    _INIT_DONE = True


# ── Çekirdek ──────────────────────────────────────────────────────────────────

_MAKS_DENEME = 2


async def _uret_brief(tip: str, gun: date) -> dict[str, Any]:
    """EC veya MG brief'i üretir; her deneme kendi session'ında.

    Hata → status='error' dict döner (istisna fırlatmaz).
    """
    from app.db.session import SessionLocal
    from sqlalchemy import text

    hata_msg = ""
    for deneme in range(1, _MAKS_DENEME + 1):
        try:
            async with SessionLocal() as session:
                if tip == "EC":
                    from app.services.brief_ec_service import generate_ec_brief
                    return await generate_ec_brief(session, gun)
                else:
                    from app.services.brief_mg_service import generate_mg_brief
                    return await generate_mg_brief(session, gun)
        except Exception as exc:
            hata_msg = str(exc)
            log.warning(
                "brief_daily.retry",
                extra={"tip": tip, "deneme": deneme, "hata": hata_msg[:200]},
            )
            if deneme < _MAKS_DENEME:
                await asyncio.sleep(5 * deneme)

    # Tüm denemeler başarısız → error kaydı yeni session'da
    full_msg = f"{tip} brief üretimi başarısız: {hata_msg}"
    try:
        async with SessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO brief_history
                        (profile_id, brief_date, generated_at, generation_ms,
                         brief_type, status, html_content,
                         kpi_data, top_insights)
                    VALUES
                        (1, :brief_date, NOW(), 0,
                         :tip, 'error', :html,
                         CAST('{}' AS jsonb), CAST('[]' AS jsonb))
                    ON CONFLICT DO NOTHING
                """),
                {
                    "brief_date": gun,
                    "tip":        tip,
                    "html":       _hata_html(tip, full_msg),
                },
            )
            await session.commit()
    except Exception as db_exc:
        log.error("brief_daily.error_kayit_basarisiz",
                  extra={"hata": str(db_exc)})

    return {"status": "error", "brief_date": gun.isoformat(), "message": full_msg}


def _hata_html(tip: str, mesaj: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        "<body style='background:#0d0d12;color:#f0f0f8;font-family:sans-serif;"
        "padding:40px;max-width:680px;margin:auto'>"
        f"<h2 style='color:#c9a961'>adL {tip} Brief — Hata</h2>"
        f"<p style='color:#ef4444'>{mesaj}</p>"
        "</body></html>"
    )


async def run_brief_daily(gun: date | None = None) -> dict[str, Any]:
    """EC + MG brief üretir. APScheduler veya doğrudan çağrı için.

    Args:
        gun: Brief tarihi. None → dün.

    Returns:
        {"ec": {...}, "mg": {...}, "elapsed_ms": int}
    """
    _lazy_init()

    if gun is None:
        gun = date.today() - timedelta(days=1)

    t0 = time.monotonic()
    log.info("brief_daily.started", extra={"gun": gun.isoformat()})

    # Kategori önbelleğini önceden ısıt (paralel çağrılar için)
    try:
        from app.utils.pimland_category import warm_category_cache
        await warm_category_cache()
        log.info("brief_daily.category_cache_warmed")
    except Exception as exc:
        log.warning("brief_daily.category_cache_warn", extra={"hata": str(exc)})

    # Her brief kendi session'ını _uret_brief içinde yönetir.
    ec_result = await _uret_brief("EC", gun)
    mg_result = await _uret_brief("MG", gun)

    elapsed = int((time.monotonic() - t0) * 1000)

    ozet = {
        "ec": {k: v for k, v in ec_result.items() if k != "html"},
        "mg": {k: v for k, v in mg_result.items() if k != "html"},
        "elapsed_ms": elapsed,
    }

    log.info(
        "brief_daily.done",
        extra={
            "gun":        gun.isoformat(),
            "ec_status":  ec_result.get("status"),
            "mg_status":  mg_result.get("status"),
            "elapsed_ms": elapsed,
        },
    )
    return ozet


# ── Lambda entry point ────────────────────────────────────────────────────────

def handler(event: dict, context: Any) -> dict:
    """AWS Lambda handler.

    EventBridge rule örneği:
        cron(0 3 * * ? *)   →  her gün 03:00 UTC (06:00 İstanbul)

    event keys (hepsi opsiyonel):
        gun: "YYYY-MM-DD"   →  zorla tarih
    """
    _lazy_init()

    gun: Optional[date] = None
    if isinstance(event, dict) and event.get("gun"):
        try:
            gun = date.fromisoformat(event["gun"])
        except ValueError:
            log.warning("brief_daily.lambda_invalid_gun", extra={"gun": event["gun"]})

    result = asyncio.run(run_brief_daily(gun))

    return {
        "statusCode": 200,
        "body": json.dumps(result, default=str),
    }


# ── CLI çalıştırma ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    gun_arg: date | None = None
    if len(sys.argv) > 1:
        try:
            gun_arg = date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"Geçersiz tarih: {sys.argv[1]} (YYYY-MM-DD bekleniyor)")
            sys.exit(1)

    sonuc = asyncio.run(run_brief_daily(gun_arg))
    print(json.dumps(sonuc, indent=2, default=str))
