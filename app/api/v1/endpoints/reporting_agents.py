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

    # Veriyi çek ve insight üret — bölüme göre odaklı prompt
    insight_question = _insight_question(payload.report_ctx)

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

def _insight_question(report_ctx: str) -> str:
    """Her bölüme özel insight sorusu — kıdemli finans analist tonu, 7-8 kart."""
    TONE = (
        "Sen kıdemli bir perakende finans direktörüsün; kurumsal müşterilere brifing yapıyor gibi "
        "net, otoriter ve aksiyon odaklı yaz. Veri varsa somut rakam belirt, "
        "önceki dönemle kıyasla, portföy riski ve fırsat penceresi kavramlarını kullan. "
        "Başlık kısa ve güçlü olsun (eylem fiili ile başla). "
        "Açıklama spesifik, bağlam içersin — genel laflardan kaçın."
    )
    JSON_FORMAT = (
        'Sadece JSON array döndür, başka hiçbir metin ekleme. Format: '
        '[{"tur":"basari|risk|trend|firsat|dikkat|bilgi","baslik":"max 10 kelime","aciklama":"1-2 cümle, spesifik veri ve aksiyon önerisi"}]'
    )
    ctx_map = {
        # ── Satış / Mağaza ────────────────────────────────────────────────────
        "magaza-yonetici": (
            f"{TONE} "
            "Mağaza ağının hedef gerçekleşme oranı, MDO performansı, OBF trendi, "
            "bölgesel sapma ve ziyaretçi dönüşüm dinamiklerini derinlemesine analiz et. "
            "Kritik aksiyon gerektiren mağaza segmentlerini, hedef aşan bölgeleri ve "
            "büyüme momentum'unu değerlendir. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "magaza-performans": (
            f"{TONE} "
            "Mağaza performans segmentasyonunu analiz et: hedef üstü/altı mağazalar, "
            "MDO ve OBF'teki portföy dağılımı, ziyaretçi trafiği kalitesi, "
            "iade oranı sapmaları ve bölgesel konsantrasyon riski. "
            "Yatırım önceliği ve kapat/geliştir kararına girdi sağlayacak bulgular üret. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "magaza-donemseel": (
            f"{TONE} "
            "Aylık ciro/hedef trend çizgisini, büyüme ivmesini (acceleration/deceleration), "
            "sezonsal anomalileri, MDO bandı kaymasını ve çeyreklik momentum'u analiz et. "
            "Düşük performanslı aylardaki yapısal nedenleri ve yıl sonu projeksiyon riskini "
            "değerlendir. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "magaza-karsilastirma": (
            f"{TONE} "
            "2024→2025→2026 yıllık büyüme dinamiklerini analiz et: YoY ciro büyüme oranı, "
            "hedef gerçekleşme eğrisi, çeyreklik performans farkları, MDO ve OBF'teki "
            "çok yıllı trendler. Büyüme ivmesi kazanan/kaybeden dönemleri ve yapısal "
            "dönüşüm sinyallerini belgele. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "magaza-gunluk": (
            f"{TONE} "
            "Son dönem günlük satış koridoru, dünkü sapma, haftalık birikim trendi, "
            "top mağazalardaki performans değişimi ve ürün bazlı momentum'u analiz et. "
            "Anlık aksiyon gerektiren sinyalleri ve olağan dışı hareketleri öne çıkar. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "magaza-gunluk-analiz": (
            f"{TONE} "
            "Son 15 günlük mağaza satış koridorunu gün gün analiz et: "
            "en güçlü/zayıf günler ve nedenleri, hafta içi/hafta sonu performans farkı, "
            "gün-üstü-gün ivme değişimi, iade trendi, aktif mağaza sayısı dalgalanması. "
            "Dikkat çeken anomalileri ve aksiyon gerektiren sinyalleri öne çıkar. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        # ── E-Ticaret ─────────────────────────────────────────────────────────
        "exec": (
            f"{TONE} "
            "Net ciro, brüt marj etkisi, iade-iptal yükü, kanal büyüme dağılımı ve "
            "OBF sağlığını executive perspektiften analiz et. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "kpi": (
            f"{TONE} "
            "KPI dashboard anomalilerini analiz et: net ciro sapması, iade artış sinyalleri, "
            "iptal trendi ve OBF erozyon riski. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "overview": (
            f"{TONE} "
            "Kanal bazlı satış dağılımı, iade oranları ve aylık trend kırılımlarını analiz et. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "kategori": (
            f"{TONE} "
            "Ürün grubu bazında net ciro payı, büyüme marjı ve iade oranı anomalilerini analiz et. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "urunler": (
            f"{TONE} "
            "Top ürünlerdeki yoğunlaşma riski, yüksek iade riskli SKUlar ve hız kazanan yeni girişler. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "iade": (
            f"{TONE} "
            "İade dinamikleri: kanal, ürün grubu ve zaman bazlı anomaliler ile finansal etki. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        # ── Ürün Yönetimi ─────────────────────────────────────────────────────
        "urun-yonetimi": (
            f"{TONE} "
            "PLM portföy durumu: marka/sezon/tema dağılımı, blokaj oranı ve internet aktivasyon sağlığı. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "plm-katalog": (
            f"{TONE} "
            "Katalog sağlığı: SKU derinliği, sezon kapsaması ve tema konsantrasyon riski. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "urun-satis": (
            f"{TONE} "
            "PLM ürünlerin satış verimliliği, tema bazlı iade oranları ve portföy optimizasyon fırsatları. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        # ── Enrichment ────────────────────────────────────────────────────────
        "enrichment": (
            f"{TONE} "
            "Ürün kalite puanı: grade dağılımı, kritik eksik alanlar ve gelir kaybı riski. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "enrichment-dashboard": (
            f"{TONE} "
            "Sezon kalite özeti: ortalama puan, yayına hazır oran ve top sorunlu alanlar. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
        "enrichment-scorelist": (
            f"{TONE} "
            "Grade D/F ürünler: satış hacmine göre öncelik sıralaması ve acil aksiyon planı. "
            f"7-8 bulgunu {JSON_FORMAT}"
        ),
    }
    return ctx_map.get(
        report_ctx,
        f"{TONE} Bu rapor için en kritik 7-8 bulguyu {JSON_FORMAT}"
    )


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
        for item in data[:8]:
            if isinstance(item, dict):
                cards.append(InsightCard(
                    tur=item.get("tur", "bilgi"),
                    baslik=item.get("baslik", "")[:80],
                    aciklama=item.get("aciklama", "")[:250],
                ))
        return cards if cards else [InsightCard(tur="bilgi", baslik="Veri yüklendi",
                                                aciklama="Soru sorun, analiz başlasın.")]
    except Exception:
        return [InsightCard(tur="bilgi", baslik="AI analiz hazır",
                            aciklama="Sorunuzu yazın, veriye dayalı yorum alın.")]
