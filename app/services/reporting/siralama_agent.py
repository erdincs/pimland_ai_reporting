"""Sıralama Agent — kategori sıralama skoru ve kural analizi. [ÇAĞIRAN]"""
from __future__ import annotations
import json, time
from typing import Any, Dict, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.agent.llm_client import llm_client
from app.services.reporting.utils.date_context import get_date_context

SIRALAMA_SYSTEM = """\
Sen Pimland'ın kategori sıralama uzmanısın.
AI sıralama skorları, kural uygulamaları (anti-monotony, fiyat çapası) ve geçmiş kararlar.
Türkçe · kriter odaklı · karar desteği.

## Veriler
{veri}
"""

async def run_siralama_agent(session: AsyncSession, question: str,
                              filters: Dict, history: List) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        r = await session.execute(text("""
            SELECT sezon_kodu, marka_adi, kategori, onayli, created_at
            FROM siralama_gecmisi ORDER BY created_at DESC LIMIT 10
        """))
        rows = [dict(row) for row in r.mappings().all()]
        for row in rows:
            if row.get("created_at"):
                row["created_at"] = row["created_at"].isoformat()
    except Exception:
        rows = []
    ctx = {"son_siralama_calismalari": rows}
    system = get_date_context() + "\n\n" + SIRALAMA_SYSTEM.format(veri=json.dumps(ctx, ensure_ascii=False, indent=2))
    answer = await llm_client.complete(system=system, user=question, max_tokens=600,
                                       temperature=0.3, history=history[-4:])
    return {"answer": answer, "elapsed_ms": round((time.perf_counter()-t0)*1000,1),
            "agent": "SIRALAMA_AGENT", "a2a_signal": None}
