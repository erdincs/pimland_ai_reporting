"""Daily Brief Orchestrator — schedule sorularını paralel çalıştırır, DB'ye yazar."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reporting.eticaret_agent import run_eticaret_agent
from app.services.reporting.kiyaslama_agent import run_kiyaslama_agent
from app.services.reporting.magaza_agent import run_magaza_agent
from app.services.reporting.urun_yonetimi_agent import run_urun_yonetimi_agent
from app.services.reporting.utils.date_context import get_date_context


_AGENTS_WITH_TON = {"satis", "eticaret", "urun_yonetimi"}

_AGENT_FUNCS = {
    "satis":         run_magaza_agent,
    "eticaret":      run_eticaret_agent,
    "kiyaslama":     run_kiyaslama_agent,
    "urun_yonetimi": run_urun_yonetimi_agent,
}

_DEFAULT_FILTERS: Dict[str, Any] = {"yil": 2026}


async def generate_brief(
    schedule_id: int,
    session: AsyncSession,
    target_date: Optional[date] = None,
) -> dict:
    t0 = time.perf_counter()
    if not target_date:
        target_date = date.today()

    # Zamanlama + profil bilgisi
    row = (await session.execute(text("""
        SELECT s.*, p.id AS profile_id, p.name AS profile_name,
               p.timezone, p.tenant_id, p.is_active AS profile_active
        FROM brief_schedules s
        JOIN brief_profiles p ON p.id = s.profile_id
        WHERE s.id = :sid
    """), {"sid": schedule_id})).mappings().first()

    if not row:
        return {"hata": "Zamanlama bulunamadı"}
    schedule = dict(row)
    if not schedule.get("is_active") or not schedule.get("profile_active"):
        return {"hata": "Zamanlama veya profil pasif"}

    profile_id = schedule["profile_id"]

    q_rows = (await session.execute(text("""
        SELECT * FROM brief_questions
        WHERE schedule_id = :sid AND is_active = true
        ORDER BY
          CASE importance
            WHEN 'kritik' THEN 1 WHEN 'yuksek' THEN 2 WHEN 'orta' THEN 3 ELSE 4
          END,
          sort_order
    """), {"sid": schedule_id})).mappings().all()
    questions = [dict(q) for q in q_rows]

    weekday = target_date.isoweekday()
    active_qs = [
        q for q in questions
        if not q.get("trigger_days") or weekday in (q["trigger_days"] or [])
    ]

    grouped: Dict[str, List] = {}
    for q in active_qs:
        grouped.setdefault(q["agent"], []).append(q)

    date_ctx = get_date_context()
    tasks = [
        _run_agent_questions(agent_name, qs, session, date_ctx, schedule)
        for agent_name, qs in grouped.items()
        if agent_name in _AGENT_FUNCS
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=180,
        )
    except asyncio.TimeoutError:
        results = []

    await session.rollback()

    all_answers: List[Dict] = []
    agent_meta: Dict = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        all_answers.extend(r["answers"])
        agent_meta[r["agent"]] = {
            "soru_sayisi": len(r["answers"]),
            "sure_ms":     r["duration_ms"],
        }

    # Profil düzeyindeki checklist
    cl_rows = (await session.execute(text("""
        SELECT ci.*, cs.is_done, cs.done_at
        FROM brief_checklist_items ci
        LEFT JOIN brief_checklist_state cs
          ON cs.item_id = ci.id AND cs.check_date = :chk
        WHERE ci.profile_id = :pid AND ci.is_active = true
        ORDER BY ci.sort_order
    """), {"pid": profile_id, "chk": target_date})).mappings().all()
    checklist = [dict(c) for c in cl_rows]

    from app.services.daily_brief.composer import compose_brief
    composed = await compose_brief(
        profile=schedule,
        answers=all_answers,
        checklist=checklist,
        date_context=date_ctx,
    )

    gen_ms = int((time.perf_counter() - t0) * 1000)

    await session.execute(text("""
        INSERT INTO brief_history
          (schedule_id, profile_id, brief_date, generation_ms,
           top_insights, kpi_data, qa_results,
           checklist_state, actions, agent_metadata,
           executive_summary, estimated_cost, tenant_id)
        VALUES
          (:sid, :pid, :bdate, :gen_ms,
           CAST(:top_ins AS JSONB), CAST(:kpi AS JSONB), CAST(:qa AS JSONB),
           CAST(:cl AS JSONB), CAST(:acts AS JSONB), CAST(:meta AS JSONB),
           CAST(:exec_sum AS JSONB), :cost, :tenant)
        ON CONFLICT (schedule_id, brief_date) DO UPDATE SET
          generated_at      = NOW(),
          generation_ms     = EXCLUDED.generation_ms,
          top_insights      = EXCLUDED.top_insights,
          kpi_data          = EXCLUDED.kpi_data,
          qa_results        = EXCLUDED.qa_results,
          checklist_state   = EXCLUDED.checklist_state,
          actions           = EXCLUDED.actions,
          agent_metadata    = EXCLUDED.agent_metadata,
          executive_summary = EXCLUDED.executive_summary
    """), {
        "sid":      schedule_id,
        "pid":      profile_id,
        "bdate":    target_date,
        "gen_ms":   gen_ms,
        "top_ins":  json.dumps(composed.get("top_insights", [])),
        "kpi":      json.dumps(composed.get("kpi_data", {})),
        "qa":       json.dumps(composed.get("qa_results", [])),
        "cl":       json.dumps(checklist, default=str),
        "acts":     json.dumps(composed.get("actions", [])),
        "meta":     json.dumps(agent_meta),
        "exec_sum": json.dumps(composed.get("executive_summary", {})),
        "cost":     float(composed.get("estimated_cost", 0)),
        "tenant":   schedule.get("tenant_id", "upagon"),
    })
    await session.commit()

    return {
        "schedule_id":   schedule_id,
        "profile_id":    profile_id,
        "brief_date":    str(target_date),
        "generation_ms": gen_ms,
        "soru_sayisi":   len(all_answers),
        "agent_count":   len(agent_meta),
        "brief":         composed,
    }


async def _run_agent_questions(
    agent_name: str,
    questions: List[Dict],
    session: AsyncSession,
    date_ctx: str,
    schedule: dict,
) -> dict:
    t0 = time.perf_counter()
    fn = _AGENT_FUNCS[agent_name]
    ton = schedule.get("tone", "yonetici")
    answers = []

    for q in questions:
        try:
            if agent_name in _AGENTS_WITH_TON:
                result = await fn(session, q["question_text"], _DEFAULT_FILTERS.copy(), [], ton)
            else:
                result = await fn(session, q["question_text"], _DEFAULT_FILTERS.copy(), [])
            answers.append({
                "question_id": q["id"],
                "question":    q["question_text"],
                "agent":       agent_name,
                "importance":  q.get("importance", "orta"),
                "is_cross":    q.get("is_cross_domain", False),
                "answer":      result.get("answer", ""),
                "data":        result.get("data"),
                "sources":     result.get("sources", []),
            })
        except Exception as exc:
            answers.append({
                "question_id": q["id"],
                "question":    q["question_text"],
                "agent":       agent_name,
                "hata":        str(exc),
            })

    return {
        "agent":       agent_name,
        "answers":     answers,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }
