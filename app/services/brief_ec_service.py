"""EC Brief servisi — repo sorguları + AI composer + HTML render + DB kayıt."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Any, Optional

import boto3
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repositories.brief_ec_repo import (
    net_ciro_kanal, ga4_trafik, iade_matrisi, top_bottom_sku,
    ec_trend_daily, ec_period_kanal,
)
from app.services.brief_renderer import render_brief
from app.utils.pimland_category import get_category_map
from app.utils.tr_format import tl, pct, delta_html, delta_class

log = logging.getLogger(__name__)

_AY = {1:"Ocak",2:"Şubat",3:"Mart",4:"Nisan",5:"Mayıs",6:"Haziran",
       7:"Temmuz",8:"Ağustos",9:"Eylül",10:"Ekim",11:"Kasım",12:"Aralık"}

_SYSTEM_PROMPT = """\
ROL: adL e-ticaret satış analistinin AI versiyonu. 4 online kanalı \
(adL.com.tr, Trendyol, HepsiBurada, BOYNER) derin anlarsın. Mağaza verisine \
ASLA değinme.

GÖREV: Aşağıdaki günlük veriyi analiz et ve JSON döndür.

KURAL:
- Türkçe yaz.
- Sayılar ₺1.234.567 ve %14,7 formatında.
- Kesinlik iddia etme ("yaklaşık" veya "veriye göre").
- Önceki gün karşılaştırması zorunlu.
- Hero caption maksimum 1 cümle.
- Her bölüm yorumu 2 cümle.
- Emoji YASAK. ▲ ▼ KULLANMA — sadece ▴ ▾ ◆.
- İade sektör normu: moda TR %15–25 normal, >%30 yüksek.
- Dönüşüm: %2–4 iyi, >%5 mükemmel. Bounce: <%40 iyi, >%60 yüksek.

ÇIKTI FORMAT (sadece JSON, başka hiçbir şey):
{
  "hero_caption": "tek cümle — net ciro hareketini anlat",
  "net_ciro_yorum": "2 cümle — kanal kırılımı ve önceki gün karşılaştırması",
  "ga4_yorum": "2 cümle — trafik kalitesi; veri stale ise bunu belirt",
  "iade_yorum": "2 cümle — kritik SKU ve beden/renk paterni",
  "top_bottom_yorum": "2 cümle — top/bottom SKU değerlendirmesi",
  "oneriler": [
    {"roman": "I",   "baslik": "kısa başlık", "aciklama": "2-3 cümle aksiyon", "etki": "tahmini etki"},
    {"roman": "II",  "baslik": "...", "aciklama": "...", "etki": "..."},
    {"roman": "III", "baslik": "...", "aciklama": "...", "etki": "..."}
  ]
}"""


# ── Bedrock ───────────────────────────────────────────────────────────────────

def _bedrock():
    kw: dict[str, Any] = {"region_name": settings.bedrock_region}
    if settings.aws_access_key_id:
        kw["aws_access_key_id"]     = settings.aws_access_key_id
        kw["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("bedrock-runtime", **kw)


def _converse_sync(user_msg: str) -> str:
    model = getattr(settings, "bedrock_composer_model", None) or settings.bedrock_model_id
    resp = _bedrock().converse(
        modelId=model,
        system=[{"text": _SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_msg}]}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0.2},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


async def _compose_ai(veri: dict) -> dict:
    user_msg = "VERİ:\n" + json.dumps(veri, ensure_ascii=False, default=str)
    try:
        raw = await asyncio.to_thread(_converse_sync, user_msg)
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
            raw = m.group(1).strip() if m else raw
        return json.loads(raw)
    except Exception as exc:
        log.warning("brief_ec_service.ai_failed", extra={"err": str(exc)})
        return {
            "hero_caption":      "Veri analiz edildi.",
            "net_ciro_yorum":    "Detaylı yorum üretilemedi.",
            "ga4_yorum":         "Trafik verisi alındı.",
            "iade_yorum":        "İade matrisi hazırlandı.",
            "top_bottom_yorum":  "SKU sıralaması hazırlandı.",
            "oneriler": [
                {"roman": "I",   "baslik": "Veri kontrolü",
                 "aciklama": "AI servisi geçici olarak kullanılamıyor.",
                 "etki": "—"},
            ],
        }


# ── Alert eşiği ──────────────────────────────────────────────────────────────

def _alert_class(delta_pct: Optional[float], mode: str = "net") -> str:
    """up / down / flat — alert renk sınıfı."""
    if delta_pct is None:
        return "flat"
    if mode == "net":
        if delta_pct <= -15: return "down"
        if delta_pct >= 10:  return "up"
    return delta_class(delta_pct)


# ── Ana servis fonksiyonu ─────────────────────────────────────────────────────

async def generate_ec_brief(session: AsyncSession, gun: date, period: int = 7) -> dict[str, Any]:
    """EC brief üretir ve brief_history'ye kaydeder.

    Returns:
        {"html": str, "status": "ok"|"no_data", "brief_date": str}
    """
    t0 = time.monotonic()

    # 1 · Sıralı DB sorguları
    ciro_data    = await net_ciro_kanal(session, gun)
    ga4_data     = await ga4_trafik(session, gun)
    iade_data    = await iade_matrisi(session, gun)
    top3, bot3   = await top_bottom_sku(session, gun)
    trend_data   = await ec_trend_daily(session, gun, period)
    period_kanal = await ec_period_kanal(session, gun, period)

    if not ciro_data["kanallar"]:
        return {
            "html":       None,
            "status":     "no_data",
            "brief_date": gun.isoformat(),
            "message":    f"E-ticaret verisi kullanılamıyor: {gun} için kayıt yok.",
        }

    # 2 · Kategori haritası (Pimland API)
    cat_map = await get_category_map()

    def _slug(urun_kodu: str) -> str:
        return cat_map.get(str(urun_kodu), "urun")

    # 3 · Özet veri (AI'a gönderilecek)
    ai_input = {
        "gun":        gun.isoformat(),
        "net_ciro":   {k["satis_kanali"]: {"net": k["net_ciro"], "delta_pct": k["delta_pct"],
                                            "iade_orani": k["iade_orani"]}
                       for k in ciro_data["kanallar"]},
        "toplam_net": ciro_data["toplam"],
        "toplam_delta_pct": ciro_data["toplam_delta"],
        "ga4":        ga4_data,
        "iade_top3":  [{"urun": r["urun_adi"], "iade_orani": r["iade_orani"],
                        "beden": r["beden"], "renk": r["renk"]}
                       for r in iade_data[:3]],
        "top3_sku":   [{"urun": r["urun_adi"], "net": r["net_ciro"],
                        "delta": r["delta_pct"]} for r in top3],
        "bot3_sku":   [{"urun": r["urun_adi"], "net": r["net_ciro"],
                        "delta": r["delta_pct"]} for r in bot3],
    }

    # 4 · AI yorum
    ai = await _compose_ai(ai_input)

    # 5 · Template context
    tarih_str = f"{gun.day} {_AY[gun.month]} {gun.year}"
    hero_net   = ciro_data["toplam"]
    hero_delta = ciro_data["toplam_delta"]

    context = {
        "body_class":       "adl-ec",
        "eyebrow":          "Günlük Brief · E-Ticaret",
        "tarih_str":        tarih_str,
        "saat_str":         "06.00",
        "hero_label":       "Net Ciro · Dün",
        "hero_figure":      tl(hero_net),
        "hero_delta":       (f"+{pct(hero_delta)}" if hero_delta and hero_delta > 0
                             else pct(hero_delta)) if hero_delta is not None else None,
        "hero_delta_cls":   _alert_class(hero_delta),
        "hero_delta_tri":   ("tri-up" if hero_delta and hero_delta > 0
                             else "tri-down" if hero_delta and hero_delta < 0
                             else "tri-flat"),
        "hero_caption":     ai.get("hero_caption", ""),
        # Bölüm verileri
        "kanallar":          ciro_data["kanallar"],
        "toplam_net":        ciro_data["toplam"],
        "toplam_iade":       sum((k.get("iade") or 0) for k in ciro_data["kanallar"]),
        "toplam_iade_orani": ciro_data.get("toplam_iade_orani", 0),
        "toplam_delta":      ciro_data["toplam_delta"],
        "net_ciro_yorum":    ai.get("net_ciro_yorum", ""),
        # Trend & dönem
        "trend_data":        trend_data,
        "trend_max":         max((d["net_ciro"] for d in trend_data), default=1) or 1,
        "iade_max":          max((d["iade_adet"] for d in trend_data), default=1) or 1,
        "period_kanal":      period_kanal,
        "period_kanal_max":  max((k["net_ciro"] for k in period_kanal), default=1) or 1,
        "period":            period,
        "gun_iso":           gun.isoformat(),
        "ga4":              ga4_data,
        "ga4_yorum":        ai.get("ga4_yorum", ""),
        "iade_matrisi":     iade_data,
        "iade_yorum":       ai.get("iade_yorum", ""),
        "top3":             top3,
        "bot3":             bot3,
        "top_bottom_yorum": ai.get("top_bottom_yorum", ""),
        "oneriler":         ai.get("oneriler", []),
        "cat_slug_fn":      _slug,
    }

    # 6 · Render
    html = render_brief("brief/ec.html", context)

    # 7 · brief_history kaydı (INSERT — mevcut profile_id 1 kullanılır,
    #     schedule_id None)
    ms = int((time.monotonic() - t0) * 1000)
    try:
        await session.execute(text("""
            INSERT INTO brief_history
                (profile_id, brief_date, generated_at, generation_ms,
                 html_content, brief_type, status,
                 kpi_data, top_insights)
            VALUES
                (1, :brief_date, NOW(), :ms,
                 :html, 'EC', 'ok',
                 CAST(:kpi_data AS jsonb), CAST(:insights AS jsonb))
            ON CONFLICT DO NOTHING
        """), {
            "brief_date": gun,
            "ms":         ms,
            "html":       html,
            "kpi_data":   json.dumps({
                "toplam_net": ciro_data["toplam"],
                "toplam_delta": ciro_data["toplam_delta"],
                "kanal_sayisi": len(ciro_data["kanallar"]),
            }),
            "insights":   json.dumps(ai.get("oneriler", [])[:3]),
        })
        await session.commit()
    except Exception as exc:
        log.warning("brief_ec_service.db_insert_failed", extra={"err": str(exc)})

    return {
        "html":       html,
        "status":     "ok",
        "brief_date": gun.isoformat(),
        "gen_ms":     ms,
    }
