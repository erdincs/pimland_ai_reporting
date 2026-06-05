"""Raporlama Agent endpoint'leri — chat + otomatik insight."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Annotated, Any, Dict, List, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session
from app.core.config import settings
from app.core.logging import get_logger
from app.services.reporting.orchestrator import route_and_run

log = get_logger(__name__)
router = APIRouter(prefix="/agents/reporting", tags=["reporting-agents"])

_redis = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
INSIGHT_TTL = 1800  # 30 dk


# ── Request / Response modelleri ──────────────────────────────────────────────

class ReportingChatRequest(BaseModel):
    question: str
    report_ctx: str                        # "magaza-yonetici", "enrichment", ...
    filters: Dict[str, Any] = {}           # {yil, ay, bolge, magaza, ...}
    history: List[Dict[str, Any]] = []     # [{role, content}]
    use_cache: bool = False                # chat genellikle cache'lenmez


class ReportingChatResponse(BaseModel):
    question: str
    answer: str
    agent: str
    report_ctx: str
    elapsed_ms: float
    veri_ozeti: Optional[Dict] = None


class InsightsRequest(BaseModel):
    report_ctx: str
    filters: Dict[str, Any] = {}
    use_cache: bool = True


class InsightCard(BaseModel):
    tur: str          # basari | risk | trend | firsat | dikkat
    baslik: str
    aciklama: str


class InsightsResponse(BaseModel):
    report_ctx: str
    insights: List[InsightCard]
    elapsed_ms: float
    cached: bool = False


# ── Chat endpoint ─────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ReportingChatResponse)
async def reporting_chat(
    payload: ReportingChatRequest,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> ReportingChatResponse:
    """Rapor bağlamlı AI sohbet — aktif raporun agent'ına yönlendirir."""
    t0 = time.perf_counter()

    # History pencereleme (max 10 mesaj)
    history = payload.history[-10:] if len(payload.history) > 10 else payload.history

    result = await route_and_run(
        session=session,
        question=payload.question,
        report_ctx=payload.report_ctx,
        filters=payload.filters,
        history=history,
    )

    return ReportingChatResponse(
        question=payload.question,
        answer=result["answer"],
        agent=result.get("agent", "UNKNOWN"),
        report_ctx=payload.report_ctx,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        veri_ozeti=result.get("veri_ozeti"),
    )


# ── Insights endpoint ─────────────────────────────────────────────────────────

@router.post("/insights", response_model=InsightsResponse)
async def reporting_insights(
    payload: InsightsRequest,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> InsightsResponse:
    """Rapor sayfası yüklenince 3-5 otomatik insight kartı üretir."""
    t0 = time.perf_counter()

    # Cache kontrolü
    cache_key = _insight_key(payload.report_ctx, payload.filters)
    if payload.use_cache:
        cached = await _redis.get(cache_key)
        if cached:
            try:
                cards = json.loads(cached)
                return InsightsResponse(
                    report_ctx=payload.report_ctx,
                    insights=[InsightCard(**c) for c in cards],
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                    cached=True,
                )
            except Exception:
                pass

    # Veriyi çek ve insight üret
    insight_question = (
        f"Bu {payload.report_ctx} raporu için en kritik 4-5 bulguyu "
        f"JSON array olarak listele. Her eleman: "
        f'{{\"tur\": "basari|risk|trend|firsat|dikkat", '
        f'"baslik": "max 8 kelime", "aciklama": "max 25 kelime"}}. '
        f"Sadece JSON array döndür, başka bir şey yazma."
    )

    result = await route_and_run(
        session=session,
        question=insight_question,
        report_ctx=payload.report_ctx,
        filters=payload.filters,
        history=[],
    )

    # JSON parse
    cards = _parse_insight_json(result["answer"])

    # Cache'e yaz
    try:
        await _redis.setex(cache_key, INSIGHT_TTL, json.dumps(
            [{"tur": c.tur, "baslik": c.baslik, "aciklama": c.aciklama} for c in cards]
        ))
    except Exception:
        pass

    return InsightsResponse(
        report_ctx=payload.report_ctx,
        insights=cards,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        cached=False,
    )


# ── Yardımcı fonksiyonlar ─────────────────────────────────────────────────────

def _insight_key(report_ctx: str, filters: Dict) -> str:
    fh = hashlib.sha256(
        json.dumps(filters, sort_keys=True).encode()
    ).hexdigest()[:12]
    return f"insight:pimland:{report_ctx}:{fh}"


def _parse_insight_json(raw: str) -> List[InsightCard]:
    """LLM yanıtından insight JSON'ını parse eder, hata toleranslı."""
    import re
    # JSON array bul
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if not m:
        # Fallback: tek kart
        return [InsightCard(tur="bilgi", baslik="AI analiz hazır",
                            aciklama="Sorunuzu yazın, veriye dayalı yorum alın.")]
    try:
        data = json.loads(m.group())
        cards = []
        for item in data[:5]:
            if isinstance(item, dict):
                cards.append(InsightCard(
                    tur=item.get("tur", "bilgi"),
                    baslik=item.get("baslik", "")[:60],
                    aciklama=item.get("aciklama", "")[:120],
                ))
        return cards if cards else [InsightCard(tur="bilgi", baslik="Veri yüklendi",
                                                aciklama="Soru sorun, analiz başlasın.")]
    except Exception:
        return [InsightCard(tur="bilgi", baslik="AI analiz hazır",
                            aciklama="Sorunuzu yazın, veriye dayalı yorum alın.")]
