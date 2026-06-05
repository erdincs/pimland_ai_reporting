"""Enrichment Agent — ürün kalite puanı ve eksik alan analizi. [YAPRAK]"""
from __future__ import annotations
import json, time
from typing import Any, Dict, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.agent.llm_client import llm_client

ENRICHMENT_SYSTEM = """\
Sen Pimland'ın ürün veri kalitesi uzmanısın.
Enrichment puanı (A-F grade), eksik alan analizi, satış etkisi konularında yanıt verirsin.
YAPRAK agent: başka agenta A2A yapamazsın.
Türkçe · teknik detay · alan adlarını aç.

## Veriler
{veri}
"""

async def run_enrichment_agent(session: AsyncSession, question: str,
                                filters: Dict, history: List) -> Dict[str, Any]:
    t0 = time.perf_counter()
    sezon = filters.get("sezon") or "26-SR"
    try:
        r = await session.execute(text("""
            SELECT quality_grade, COUNT(*) n, ROUND(AVG(quality_score),1) ort_puan
            FROM enrichment_quality GROUP BY quality_grade ORDER BY quality_grade
        """))
        rows = [dict(row) for row in r.mappings().all()]
    except Exception:
        rows = []
    ctx = {"grade_dagilimi": rows, "sezon": sezon}
    system = ENRICHMENT_SYSTEM.format(veri=json.dumps(ctx, ensure_ascii=False, indent=2))
    answer = await llm_client.complete(system=system, user=question, max_tokens=600,
                                       temperature=0.3, history=history[-4:])
    return {"answer": answer, "elapsed_ms": round((time.perf_counter()-t0)*1000,1),
            "agent": "ENRICHMENT_AGENT", "a2a_signal": None}
