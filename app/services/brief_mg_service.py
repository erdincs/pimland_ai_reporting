"""MG Brief servisi — repo sorguları + AI composer + HTML render + DB kayıt."""
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
from app.repositories.brief_mg_repo import (
    ozet_kpi, bolge_muduru_tablosu, top_bottom_magaza,
    iade_matrisi_mg, lfl_ozet,
)
from app.services.brief_renderer import render_brief
from app.utils.pimland_category import get_category_map
from app.utils.tr_format import tl, pct, num, delta_html, delta_class

log = logging.getLogger(__name__)

_AY = {1:"Ocak",2:"Şubat",3:"Mart",4:"Nisan",5:"Mayıs",6:"Haziran",
       7:"Temmuz",8:"Ağustos",9:"Eylül",10:"Ekim",11:"Kasım",12:"Aralık"}

_SYSTEM_PROMPT = """\
ROL: adL fiziksel mağaza satış analistinin AI versiyonu. 102 mağaza, 15 bölge \
müdürü ağını derin anlarsın. MDO, OBF, Sepet, Ziyaretçi, Hedef uzmanısın. \
E-ticaret / online kanala ASLA değinme.

GÖREV: Aşağıdaki veriyi analiz et ve JSON döndür.

KURAL:
- Türkçe yaz. Sayılar ₺1.234.567 ve %14,7 formatında.
- Kesinlik iddia etme ("yaklaşık" veya "veriye göre").
- Hero caption maksimum 1 cümle.
- Her bölüm yorumu 2 cümle.
- Emoji YASAK. ▲ ▼ KULLANMA — sadece ▴ ▾ ◆.
- Sektör normları: MDO %12–18 (adL ~%5,91 → kritik), Sepet 2,5–3,5 (adL ~1,90 → düşük).
- Hedef: >%95 iyi | %85–95 kabul | %80–85 uyarı | <%80 kritik.
- LfL: >%5 büyüme | <%0 daralma.
- Benchmark: %1 MDO↑ ≈ ciro %6,8↑

ANALİZ ÇERÇEVESİ:
  Ziyaretçi↓ → trafik sorunu
  MDO↓       → dönüşüm sorunu (karşılama / personel)
  OBF↓       → fiyat / ürün mix sorunu
  Sepet↓     → çapraz satış sorunu

ÇIKTI FORMAT (sadece JSON, başka hiçbir şey):
{
  "hero_caption": "tek cümle — günlük net ciro ve ay genel gidişatını anlat",
  "kpi_yorum": "2 cümle — MDO×OBF×Sepet üçgenini yorumla",
  "bolge_yorum": "2 cümle — bölge müdürü tablosu değerlendirmesi",
  "top_bottom_yorum": "2 cümle — en iyi/kötü mağaza paterni",
  "iade_yorum": "2 cümle — beden paterni + ana mağaza odağı",
  "lfl_yorum": "2 cümle — YoY karşılaştırma yorumu",
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
        inferenceConfig={"maxTokens": 1400, "temperature": 0.2},
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
        log.warning("brief_mg_service.ai_failed", extra={"err": str(exc)})
        return {
            "hero_caption":   "Veri analiz edildi.",
            "kpi_yorum":      "Detaylı yorum üretilemedi.",
            "bolge_yorum":    "Bölge verisi alındı.",
            "iade_yorum":     "İade matrisi hazırlandı.",
            "top_bottom_yorum": "Mağaza sıralaması hazırlandı.",
            "lfl_yorum":      "YoY karşılaştırma hazırlandı.",
            "oneriler": [
                {"roman": "I", "baslik": "Veri kontrolü",
                 "aciklama": "AI servisi geçici olarak kullanılamıyor.",
                 "etki": "—"},
            ],
        }


# ── Yardımcılar ───────────────────────────────────────────────────────────────

def _alert_class(val: Optional[float], esik_iyi: float = 0, esik_kotu: float = 0,
                 mode: str = "pct") -> str:
    if val is None:
        return "flat"
    if val >= esik_iyi:
        return "up"
    if val <= esik_kotu:
        return "down"
    return "flat"


def _doluluk_class(doluluk: Optional[float]) -> str:
    if doluluk is None:
        return "flat"
    if doluluk >= 95:
        return "up"
    if doluluk < 80:
        return "down"
    return "flat"


# ── Ana servis ────────────────────────────────────────────────────────────────

async def generate_mg_brief(session: AsyncSession, gun: date) -> dict[str, Any]:
    """MG brief üretir ve brief_history'ye kaydeder.

    Returns:
        {"html": str, "status": "ok"|"no_data", "brief_date": str, "gen_ms": int}
    """
    t0 = time.monotonic()

    # 1 · Sıralı DB sorguları (aynı session'da paralel sorgu desteklenmiyor)
    ozet         = await ozet_kpi(session, gun)
    bolge_listesi = await bolge_muduru_tablosu(session, gun)
    top5, bot5   = await top_bottom_magaza(session, gun)
    iade_listesi = await iade_matrisi_mg(session, gun)
    lfl_data     = await lfl_ozet(session, gun)

    if not ozet["gun_net"] and not ozet["ay_mevcut"]:
        return {
            "html":       None,
            "status":     "no_data",
            "brief_date": gun.isoformat(),
            "message":    f"Mağaza satış verisi kullanılamıyor: {gun} için kayıt yok.",
        }

    # 2 · Kategori haritası (iade kartları için)
    cat_map = await get_category_map()
    def _slug(urun_kodu: str) -> str:
        return cat_map.get(str(urun_kodu), "urun")

    # 3 · AI girdi özeti
    ai_input = {
        "gun":          gun.isoformat(),
        "gun_net_ciro": ozet["gun_net"],
        "gun_delta_pct": ozet["gun_delta_pct"],
        "ay_doluluk":   ozet["ay_doluluk"],
        "mdo_pct":      ozet["mdo"],
        "sepet":        ozet["sepet"],
        "obf_tl":       ozet["obf"],
        "ziyaretci":    ozet["ziyaretci"],
        "bolge_top3":   bolge_listesi[:3] if bolge_listesi else [],
        "top5_magaza":  [{"magaza": r["magaza"], "doluluk": r["doluluk"]} for r in top5],
        "bot5_magaza":  [{"magaza": r["magaza"], "doluluk": r["doluluk"]} for r in bot5],
        "iade_top3":    [{"urun": r["urun_adi"], "beden": r["beden"],
                          "iade_orani": float(r["iade_orani"] or 0)} for r in iade_listesi[:3]],
        "lfl_pct":      lfl_data.get("lfl_pct"),
    }

    # 4 · AI yorum
    ai = await _compose_ai(ai_input)

    # 5 · Template context
    tarih_str = f"{gun.day} {_AY[gun.month]} {gun.year}"
    # Hero = aylık MTD net ciro (bölge tablosuyla tutarlı).
    # Günlük gun_net template'e ayrıca geçirilir (section 1'de gösterilir).
    hero_net   = ozet["ay_net"]   if ozet["ay_mevcut"] else ozet["gun_net"]
    hero_delta = ozet["ay_doluluk"]  # hedef doluluk % olarak

    context = {
        "body_class":    "adl-mg",
        "eyebrow":       "Günlük Brief · Mağaza",
        "tarih_str":     tarih_str,
        "saat_str":      "06.00",
        "hero_label":    "Net Ciro · Ay MTD",
        "hero_figure":   tl(hero_net) if hero_net else "—",
        "hero_delta":    (f"+{pct(hero_delta)}" if hero_delta and hero_delta > 0
                          else pct(hero_delta)) if hero_delta is not None else None,
        "hero_delta_cls": _doluluk_class(ozet["ay_doluluk"]),
        "hero_delta_tri": ("tri-up" if hero_delta and hero_delta and hero_delta >= 95
                           else "tri-down" if hero_delta and hero_delta < 80
                           else "tri-flat"),
        "hero_caption":  ai.get("hero_caption", ""),
        # Bölüm 1 — KPI üçgeni
        "ozet":          ozet,
        "kpi_yorum":     ai.get("kpi_yorum", ""),
        # Bölüm 2 — Bölge müdürü
        "bolge_listesi": bolge_listesi,
        "bolge_yorum":   ai.get("bolge_yorum", ""),
        # Bölüm 3 — Top/Bottom
        "top5":          top5,
        "bot5":          bot5,
        "top_bottom_yorum": ai.get("top_bottom_yorum", ""),
        # Bölüm 4 — İade
        "iade_matrisi":  iade_listesi,
        "iade_yorum":    ai.get("iade_yorum", ""),
        # Bölüm 5 — LfL
        "lfl":           lfl_data,
        "lfl_yorum":     ai.get("lfl_yorum", ""),
        # Bölüm 6 — Öneriler
        "oneriler":      ai.get("oneriler", []),
        # Helpers
        "cat_slug_fn":   _slug,
    }

    # 6 · Render
    html = render_brief("brief/mg.html", context)

    # 7 · brief_history kayıt
    ms = int((time.monotonic() - t0) * 1000)
    try:
        await session.execute(text("""
            INSERT INTO brief_history
                (profile_id, brief_date, generated_at, generation_ms,
                 html_content, brief_type, status,
                 kpi_data, top_insights)
            VALUES
                (1, :brief_date, NOW(), :ms,
                 :html, 'MG', 'ok',
                 CAST(:kpi_data AS jsonb), CAST(:insights AS jsonb))
            ON CONFLICT DO NOTHING
        """), {
            "brief_date": gun,
            "ms":         ms,
            "html":       html,
            "kpi_data":   json.dumps({
                "gun_net":    ozet["gun_net"],
                "ay_doluluk": ozet["ay_doluluk"],
                "mdo":        ozet["mdo"],
            }),
            "insights":   json.dumps(ai.get("oneriler", [])[:3]),
        })
        await session.commit()
    except Exception as exc:
        log.warning("brief_mg_service.db_insert_failed", extra={"err": str(exc)})

    return {
        "html":       html,
        "status":     "ok",
        "brief_date": gun.isoformat(),
        "gen_ms":     ms,
    }
