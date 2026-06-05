"""E-Ticaret Satış Agent — online kanallar, SKU performansı, iade analizi."""
from __future__ import annotations
import json, time
from typing import Any, Dict, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.agent.llm_client import llm_client

ETICARET_SYSTEM = """\
Sen Pimland'ın e-ticaret satış analisti için AI asistanısın.
Online kanallar (ADL, Trendyol, HB), SKU performansı, iade analizi.
Türkçe · kanal odaklı · dönüşüm metrikleri.

## Veriler
{veri}
"""

async def run_eticaret_agent(session: AsyncSession, question: str,
                               filters: Dict, history: List) -> Dict[str, Any]:
    t0 = time.perf_counter()
    yil = int(filters.get("yil", 2026))
    try:
        r = await session.execute(text("""
            SELECT satis_kanali, SUM(tutar) brut_ciro, SUM(adet::int) brut_adet,
                   ROUND((100.0*SUM(tutar)/NULLIF(SUM(SUM(tutar)) OVER(),0))::numeric,1) pay
            FROM incorta_satis WHERE yil=:yil
            GROUP BY satis_kanali ORDER BY brut_ciro DESC LIMIT 10
        """), {"yil": yil})
        rows = [dict(row) for row in r.mappings().all()]
    except Exception:
        rows = []
    ctx = {"yil": yil, "kanal_ozeti": rows}
    system = ETICARET_SYSTEM.format(veri=json.dumps(ctx, ensure_ascii=False, indent=2))
    answer = await llm_client.complete(system=system, user=question, max_tokens=700,
                                       temperature=0.3, history=history[-4:])
    return {"answer": answer, "elapsed_ms": round((time.perf_counter()-t0)*1000,1),
            "agent": "ETICARET_AGENT", "a2a_signal": None}
