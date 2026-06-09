"""Dönemsel Kıyaslama Agent — YoY/MoM/YTD karşılaştırma. [YAPRAK]"""
from __future__ import annotations
import decimal, json, time
from typing import Any, Dict, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.agent.llm_client import llm_client
from app.services.reporting.utils.date_context import get_date_context

def _to_native(v: Any) -> Any:
    """Decimal → float, diğerleri olduğu gibi."""
    return float(v) if isinstance(v, decimal.Decimal) else v

KIYASLAMA_SYSTEM = """\
Sen dönemsel karşılaştırma uzmanısın: YoY, MoM, YTD, çeyreklik.
YAPRAK agent: başka agenta A2A yapamazsın.
Her projeksiyona "tahmini" etiketi zorunlu.
Sayılarda Türk formatı: 1.234.567 ₺ — MDO değerleri yüzde olarak yaz.

## Mevcut veriler
Aşağıdaki veri mağaza ağının AY BAZLI toplam performansını içerir.
Her satırda: yıl, ay, toplam ciro, hedef, ziyaretçi, ortalama MDO (%), OBF, mağaza sayısı.

{veri}
"""

async def run_kiyaslama_agent(session: AsyncSession, question: str,
                               filters: Dict, history: List) -> Dict[str, Any]:
    t0 = time.perf_counter()
    bolge  = (filters.get("bolge") or "").strip() or None
    magaza = (filters.get("magaza") or "").strip() or None

    conds = ["magaza IS NOT NULL", "TRIM(magaza) <> ''"]
    params: Dict[str, Any] = {}
    if bolge:
        conds.append("bolge_muduru ILIKE :bolge")
        params["bolge"] = f"%{bolge}%"
    if magaza:
        conds.append("magaza ILIKE :magaza")
        params["magaza"] = f"%{magaza}%"
    where = " AND ".join(conds)

    try:
        r = await session.execute(text(f"""
            SELECT
                yil::integer AS yil,
                ay::integer  AS ay,
                ROUND(SUM(CASE WHEN net_ciro::text  NOT IN ('--','') THEN net_ciro::float  ELSE 0 END)::numeric)  AS ciro,
                ROUND(SUM(CASE WHEN hedef::text     NOT IN ('--','') THEN hedef::float     ELSE 0 END)::numeric)  AS hedef,
                ROUND(SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END)::numeric)  AS ziyaretci,
                ROUND(
                    (CASE
                        WHEN SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END) > 0
                        THEN SUM(CASE WHEN mdo::text NOT IN ('--','') AND ziyaretci::text NOT IN ('--','')
                                      THEN mdo::float * ziyaretci::float ELSE 0 END)
                             / SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END) * 100
                        ELSE 0
                     END)::numeric, 1
                ) AS mdo_pct,
                ROUND(
                    (CASE
                        WHEN SUM(CASE WHEN net_adet::text NOT IN ('--','') THEN net_adet::float ELSE 0 END) > 0
                        THEN SUM(CASE WHEN net_ciro::text NOT IN ('--','') THEN net_ciro::float ELSE 0 END)
                             / SUM(CASE WHEN net_adet::text NOT IN ('--','') THEN net_adet::float ELSE 0 END)
                        ELSE 0
                     END)::numeric
                ) AS ort_obf,
                COUNT(DISTINCT magaza) AS magaza_sayisi
            FROM incorta_magaza_performans
            WHERE {where}
            GROUP BY yil::integer, ay::integer
            ORDER BY yil, ay
        """), params or None)
        rows = [{k: _to_native(v) for k, v in row.items()} for row in r.mappings().all()]
    except Exception:
        rows = []
    ctx = {"aylik_trend_son_24_ay": rows[-24:]}
    system = get_date_context() + "\n\n" + KIYASLAMA_SYSTEM.format(veri=json.dumps(ctx, ensure_ascii=False, indent=2))
    answer = await llm_client.complete(system=system, user=question, max_tokens=700,
                                       temperature=0.3, history=history[-4:])
    return {"answer": answer, "elapsed_ms": round((time.perf_counter()-t0)*1000,1),
            "agent": "KIYASLAMA_AGENT", "a2a_signal": None}
