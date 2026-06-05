"""Raporlama Orchestrator — deterministik routing + A2A koordinasyonu."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)

# ── Routing tablosu ───────────────────────────────────────────────────────────
ROUTING_MAP: Dict[str, str] = {
    # Satış Analiz → Mağaza
    "magaza-yonetici":       "magaza",
    "magaza-performans":     "magaza",
    "magaza-donemseel":      "magaza",
    "magaza-karsilastirma":  "magaza",
    # Zenginleştirme
    "enrichment":            "enrichment",
    "enrichment-dashboard":  "enrichment",
    "enrichment-scorelist":  "enrichment",
    "enrichment-products":   "enrichment",
    # Kategori Planlama
    "siralama":              "siralama",
    # E-Ticaret
    "exec":                  "eticaret",
    "kpi":                   "eticaret",
    "overview":              "eticaret",
    "kategori":              "eticaret",
    "urunler":               "eticaret",
    "iade":                  "eticaret",
    "colors":                "eticaret",
    "urun-satis":            "eticaret",
    "urun-yonetimi":         "eticaret",
}

# Ton haritası (report_ctx → ton)
TON_MAP: Dict[str, str] = {
    "magaza-yonetici":      "yonetici",
    "magaza-performans":    "operasyonel",
    "magaza-donemseel":     "analitik",
    "magaza-karsilastirma": "stratejik",
    "enrichment":           "teknik",
    "siralama":             "teknik",
    "eticaret":             "operasyonel",
}


async def route_and_run(
    session: AsyncSession,
    question: str,
    report_ctx: str,
    filters: Dict[str, Any],
    history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Soruyu doğru agent'a yönlendir ve yanıtı döndür."""
    agent_key = ROUTING_MAP.get(report_ctx, "magaza")
    ton       = TON_MAP.get(report_ctx, "yonetici")

    log.info("orchestrator.route", report_ctx=report_ctx, agent=agent_key)

    if agent_key == "magaza":
        from app.services.reporting.magaza_agent import run_magaza_agent
        result = await run_magaza_agent(session, question, filters, history, ton)

    elif agent_key == "enrichment":
        from app.services.reporting.enrichment_agent import run_enrichment_agent
        result = await run_enrichment_agent(session, question, filters, history)

    elif agent_key == "siralama":
        from app.services.reporting.siralama_agent import run_siralama_agent
        result = await run_siralama_agent(session, question, filters, history)

    elif agent_key == "eticaret":
        from app.services.reporting.eticaret_agent import run_eticaret_agent
        result = await run_eticaret_agent(session, question, filters, history)

    else:
        result = {
            "answer": "Bu rapor için henüz AI analiz desteği eklenmedi.",
            "elapsed_ms": 0, "agent": "UNKNOWN", "a2a_signal": None,
        }

    # A2A sinyal varsa yaprak agent'ı çağır (max 1 hop)
    if result.get("a2a_signal"):
        sig = result["a2a_signal"]
        target = sig.get("hedef_agent", "").lower()
        sub_q  = sig.get("soru", question)
        log.info("orchestrator.a2a", caller=agent_key, callee=target)

        if target in ("kiyaslama_agent", "kiyaslama"):
            from app.services.reporting.kiyaslama_agent import run_kiyaslama_agent
            leaf = await run_kiyaslama_agent(session, sub_q, filters, [])
            result["answer"] += f"\n\n---\n**Dönemsel Karşılaştırma:** {leaf['answer']}"

        elif target in ("enrichment_agent", "enrichment"):
            from app.services.reporting.enrichment_agent import run_enrichment_agent
            leaf = await run_enrichment_agent(session, sub_q, filters, [])
            result["answer"] += f"\n\n---\n**Ürün Kalitesi:** {leaf['answer']}"

    return result
