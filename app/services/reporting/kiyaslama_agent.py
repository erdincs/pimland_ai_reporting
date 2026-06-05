"""Dönemsel Kıyaslama Agent — YoY/MoM/YTD karşılaştırma. [YAPRAK]"""
from __future__ import annotations
import json, time
from typing import Any, Dict, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.agent.llm_client import llm_client

KIYASLAMA_SYSTEM = """\
Sen dönemsel karşılaştırma uzmanısın: YoY, MoM, YTD, çeyreklik.
YAPRAK agent: başka agenta A2A yapamazsın.
Her projeksiyona "tahmini" etiketi zorunlu.

## Veriler
{veri}
"""

async def run_kiyaslama_agent(session: AsyncSession, question: str,
                               filters: Dict, history: List) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        r = await session.execute(text("""
            SELECT yil::integer, ay::integer,
                   SUM(CASE WHEN net_ciro::text NOT IN ('--','') THEN net_ciro::float ELSE 0 END) ciro,
                   SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END) ziy
            FROM incorta_magaza_performans
            WHERE magaza IS NOT NULL AND TRIM(magaza)<>''
            GROUP BY yil::integer, ay::integer ORDER BY yil, ay
        """))
        rows = [dict(row) for row in r.mappings().all()]
    except Exception:
        rows = []
    ctx = {"aylik_trend": rows[-24:]}
    system = KIYASLAMA_SYSTEM.format(veri=json.dumps(ctx, ensure_ascii=False, indent=2))
    answer = await llm_client.complete(system=system, user=question, max_tokens=600,
                                       temperature=0.3, history=history[-4:])
    return {"answer": answer, "elapsed_ms": round((time.perf_counter()-t0)*1000,1),
            "agent": "KIYASLAMA_AGENT", "a2a_signal": None}
