"""Brief Composer — sub-agent yanıtlarından top insights + aksiyonlar + exec özet üretir."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import Any, Dict, List, Optional

import boto3

from app.core.config import settings


TONE_INSTRUCTIONS = {
    "yonetici":    "Stratejik, kısa cümleli, karar odaklı. Yüksek seviye bakış.",
    "operasyonel": "Detaylı veri, somut sayılar, hemen eyleme dönülebilir öneriler.",
    "analitik":    "Trend analizi, derinlemesine veri yorumu, sebep-sonuç ilişkileri.",
}

_AY_ADI = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}
_GUN_ADI = {
    0: "Pazartesi", 1: "Salı", 2: "Çarşamba", 3: "Perşembe",
    4: "Cuma", 5: "Cumartesi", 6: "Pazar",
}


def _bedrock_client():
    kwargs: Dict[str, Any] = {"region_name": settings.bedrock_region}
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("bedrock-runtime", **kwargs)


def _converse_sync(system_prompt: str, user_message: str, max_tokens: int, temperature: float) -> str:
    client = _bedrock_client()
    resp = client.converse(
        modelId=settings.bedrock_model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


async def _converse(system_prompt: str, user_message: str, max_tokens: int = 800, temperature: float = 0.3) -> str:
    return await asyncio.to_thread(_converse_sync, system_prompt, user_message, max_tokens, temperature)


def _parse_json(text: str, fallback: Any) -> Any:
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        text = m.group(1).strip() if m else text.split("```")[1].strip()
    try:
        return json.loads(text.strip())
    except Exception:
        return fallback


async def compose_brief(
    profile: dict,
    answers: List[Dict],
    checklist: List[Dict],
    date_context: str,
) -> dict:
    tone = profile.get("tone", "yonetici")
    top_n = profile.get("top_insight_count", 5)

    today = date.today()
    tarih_str = f"{_GUN_ADI[today.weekday()]}, {today.day} {_AY_ADI[today.month]} {today.year}"

    top_insights, actions, exec_summary = await asyncio.gather(
        _extract_top_insights(answers, top_n, tone),
        _generate_actions(answers),
        _generate_executive_summary(answers, profile, tarih_str),
        return_exceptions=True,
    )
    if isinstance(top_insights, Exception):
        top_insights = []
    if isinstance(actions, Exception):
        actions = []
    if isinstance(exec_summary, Exception):
        exec_summary = {}

    return {
        "executive_summary": exec_summary,
        "top_insights":      top_insights,
        "kpi_data":          _extract_kpis(answers),
        "qa_results":        _format_qa_cards(answers),
        "actions":           actions,
        "estimated_cost":    0.08,
        "tone":              tone,
    }


async def _generate_executive_summary(
    answers: List[Dict],
    profile: dict,
    tarih_str: str,
) -> Dict:
    """Günlük yönetici özeti — KPI strip + tek paragraf + kritik uyarı + odak."""
    valid = [a for a in answers if "hata" not in a]
    if not valid:
        return {}

    tone = profile.get("tone", "yonetici")
    profile_name = profile.get("profile_name") or profile.get("name", "Yönetici")

    kritik = [a for a in valid if a.get("importance") == "kritik"]
    digest = "\n\n".join([
        f"[{a.get('importance','orta').upper()}] {a['question']}\n{a['answer'][:400]}"
        for a in (kritik + [a for a in valid if a not in kritik])[:12]
    ])

    system = f"""\
Sen Pimland yönetim brief asistanısın.
Verilen Q&A özetinden bir YÖNETİCİ ÖZET JSON üret.
Profil: {profile_name}
Tarih: {tarih_str}
Ton: {TONE_INSTRUCTIONS.get(tone, '')}

KURALLAR:
- ozet_metin: 2-3 cümle, sayılar önde, kritik bulgu önde.
- kpi_strip: en fazla 4 KPI. deger kısa (ör. "1.64B ₺" veya "%70"). trend: "up"/"down"/"flat".
- kritik_uyari: tek cümle, en acil sorun. Yoksa null.
- bugun_odak: bugün yapılması gereken 1 somut öncelik.

SADECE JSON döndür:
{{
  "ozet_metin": "...",
  "kpi_strip": [{{"etiket":"...","deger":"...","delta":"...","trend":"up"}}],
  "kritik_uyari": "..." | null,
  "bugun_odak": "..."
}}"""

    text = await _converse(system, digest, max_tokens=700, temperature=0.25)
    result = _parse_json(text, {})
    result["tarih"] = tarih_str
    return result


async def _extract_top_insights(answers: List[Dict], top_n: int, tone: str) -> List[Dict]:
    sorted_answers = sorted(
        [a for a in answers if "hata" not in a],
        key=lambda x: {"kritik": 1, "yuksek": 2, "orta": 3, "dusuk": 4}.get(x.get("importance", "orta"), 5),
    )
    if not sorted_answers:
        return []

    answer_summary = "\n\n".join([
        f"[{a.get('importance','orta').upper()}] {a['question']}\n→ {a['answer'][:300]}"
        for a in sorted_answers[:14]
    ])

    tone_note = TONE_INSTRUCTIONS.get(tone, "")
    system = (
        f"Sen Pimland yönetim brief'i hazırlayan AI asistanısın.\n"
        f"Aşağıdaki soru-cevaplardan en kritik {top_n} insight'ı çıkar.\n"
        f"TON: {tone_note}\n"
        f"- Her insight 1-2 cümle\n"
        f"- Sayı/oran varsa MUTLAKA göster\n"
        f"- Karşıt durumlar varsa belirt\n"
        f"- Aksiyon ima eden cümleler kullan\n"
        f"YANIT — SADECE JSON (başka hiçbir şey yok):\n"
        f'{{"insights":[{{"num":1,"text":"..."}},{{"num":2,"text":"..."}}]}}'
    )

    text = await _converse(system, answer_summary, max_tokens=1200, temperature=0.3)
    result = _parse_json(text, {})
    return result.get("insights", [])


def _extract_kpis(answers: List[Dict]) -> Dict:
    kpi: Dict = {}
    for a in answers:
        data = a.get("data") or {}
        if a.get("agent") == "satis":
            if "toplam_net_ciro" in data:
                kpi.setdefault("ciro", data["toplam_net_ciro"])
            if "hedef_oran" in data:
                kpi.setdefault("hedef", data["hedef_oran"])
        elif a.get("agent") == "eticaret":
            if "donusum_orani" in data:
                kpi.setdefault("donusum", data["donusum_orani"])
            if "iade_orani" in data:
                kpi.setdefault("iade", data["iade_orani"])
            for k in ("brut_ciro", "net_ciro", "iade_oran_pct", "net_obf"):
                if k in data:
                    kpi.setdefault(k, data[k])
    return kpi


def _format_qa_cards(answers: List[Dict]) -> List[Dict]:
    return [
        {
            "question":   a["question"],
            "agent":      a["agent"],
            "importance": a.get("importance", "orta"),
            "is_cross":   a.get("is_cross", False),
            "answer":     a["answer"],
            "data":       a.get("data"),
            "sources":    a.get("sources", []),
        }
        for a in answers
        if "hata" not in a
    ]


async def _generate_actions(answers: List[Dict]) -> List[Dict]:
    valid = [a for a in answers if "hata" not in a]
    if not valid:
        return []

    context = "\n".join([
        f"- [{a['agent']}|{a.get('importance','orta')}] {a['answer'][:200]}"
        for a in valid[:10]
    ])

    system = (
        "Yöneticiye somut aksiyon önerileri çıkar. EN FAZLA 4 AKSIYON.\n"
        "Her aksiyon: ne yapılacak, kim/ne hakkında, beklenen sonuç.\n"
        'YANIT — SADECE JSON:\n'
        '{"actions":[{"title":"Kısa başlık","description":"Açıklama (1-2 cümle)","button_text":"Başlat"}]}'
    )

    text = await _converse(system, context, max_tokens=600, temperature=0.4)
    result = _parse_json(text, {})
    return result.get("actions", [])
