"""Brief Composer — sub-agent yanıtlarından top insights + aksiyonlar üretir."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import boto3

from app.core.config import settings


TONE_INSTRUCTIONS = {
    "yonetici":    "Stratejik, kısa cümleli, karar odaklı. Yüksek seviye bakış.",
    "operasyonel": "Detaylı veri, somut sayılar, hemen eyleme dönülebilir öneriler.",
    "analitik":    "Trend analizi, derinlemesine veri yorumu, sebep-sonuç ilişkileri.",
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


async def compose_brief(
    profile: dict,
    answers: List[Dict],
    checklist: List[Dict],
    date_context: str,
) -> dict:
    tone = profile.get("tone", "yonetici")
    top_n = profile.get("top_insight_count", 3)

    top_insights, actions = await asyncio.gather(
        _extract_top_insights(answers, top_n, tone),
        _generate_actions(answers),
        return_exceptions=True,
    )
    if isinstance(top_insights, Exception):
        top_insights = []
    if isinstance(actions, Exception):
        actions = []

    return {
        "top_insights": top_insights,
        "kpi_data":     _extract_kpis(answers),
        "qa_results":   _format_qa_cards(answers),
        "actions":      actions,
        "estimated_cost": 0.08,
        "tone": tone,
    }


async def _extract_top_insights(answers: List[Dict], top_n: int, tone: str) -> List[Dict]:
    sorted_answers = sorted(
        [a for a in answers if "hata" not in a],
        key=lambda x: {"kritik": 1, "yuksek": 2, "orta": 3, "dusuk": 4}.get(x.get("importance", "orta"), 5),
    )
    if not sorted_answers:
        return []

    answer_summary = "\n\n".join([
        f"[{a.get('importance','orta').upper()}] {a['question']}\n→ {a['answer'][:300]}"
        for a in sorted_answers[:10]
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
        f'{{"insights":[{{"num":1,"text":"..."}},{{"num":2,"text":"..."}},{{"num":3,"text":"..."}}]}}'
    )

    text = await _converse(system, answer_summary, max_tokens=900, temperature=0.3)
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip()).get("insights", [])
    except Exception:
        return []


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
        for a in valid[:8]
    ])

    system = (
        "Yöneticiye somut aksiyon önerileri çıkar. EN FAZLA 3 AKSIYON.\n"
        "Her aksiyon: ne yapılacak, kim/ne hakkında, beklenen sonuç.\n"
        'YANIT — SADECE JSON:\n'
        '{"actions":[{"title":"Kısa başlık","description":"Açıklama (1-2 cümle)","button_text":"Başlat"}]}'
    )

    text = await _converse(system, context, max_tokens=500, temperature=0.4)
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip()).get("actions", [])
    except Exception:
        return []
