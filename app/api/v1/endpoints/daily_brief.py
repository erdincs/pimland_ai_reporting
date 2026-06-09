"""Daily Brief — profil yönetim ve brief üretim endpoint'leri."""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session, get_session

router = APIRouter(prefix="/daily-brief", tags=["Daily Brief"])

_TENANT = "upagon"


# ── helpers ──────────────────────────────────────────────────────────────────

def _j(v: Any) -> str:
    """Python object → JSON string for CAST(:x AS JSONB)."""
    if v is None:
        return "null"
    return json.dumps(v, default=str)


def _t(v: Any):
    """String '07:00' → datetime.time for asyncpg TIME columns."""
    from datetime import time as dt_time
    if v is None:
        return None
    if isinstance(v, dt_time):
        return v
    h, m = str(v).split(":")[:2]
    return dt_time(int(h), int(m))


# ── PROFIL CRUD ───────────────────────────────────────────────────────────────

@router.get("/profiles")
async def list_profiles(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    only_active: bool = Query(default=False),
) -> dict:
    where = "WHERE p.tenant_id = :tenant"
    params: Dict[str, Any] = {"tenant": _TENANT}
    if only_active:
        where += " AND p.is_active = true"

    rows = (await session.execute(text(f"""
        SELECT p.*,
               (SELECT COUNT(*) FROM brief_questions q
                WHERE q.profile_id = p.id AND q.is_active = true) AS question_count,
               (SELECT COUNT(*) FROM brief_checklist_items c
                WHERE c.profile_id = p.id AND c.is_active = true) AS checklist_count,
               (SELECT generated_at FROM brief_history h
                WHERE h.profile_id = p.id ORDER BY brief_date DESC LIMIT 1) AS last_brief_at
        FROM brief_profiles p
        {where}
        ORDER BY p.created_at
    """), params)).mappings().all()

    return {"profiles": [dict(r) for r in rows]}


@router.get("/profiles/{profile_id}")
async def get_profile(
    profile_id: int,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    profile = (await session.execute(
        text("SELECT * FROM brief_profiles WHERE id = :pid"),
        {"pid": profile_id},
    )).mappings().first()
    if not profile:
        raise HTTPException(404, "Profil bulunamadı")

    questions = (await session.execute(text("""
        SELECT * FROM brief_questions
        WHERE profile_id = :pid AND is_active = true
        ORDER BY sort_order, id
    """), {"pid": profile_id})).mappings().all()

    checklist = (await session.execute(text("""
        SELECT * FROM brief_checklist_items
        WHERE profile_id = :pid AND is_active = true
        ORDER BY sort_order, id
    """), {"pid": profile_id})).mappings().all()

    return {
        "profile":   dict(profile),
        "questions": [dict(q) for q in questions],
        "checklist": [dict(c) for c in checklist],
    }


@router.post("/profiles")
async def create_profile(
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    pid = payload.get("profile_id") or f"profile_{int(time.time())}"
    result = await session.execute(text("""
        INSERT INTO brief_profiles
            (profile_id, name, role, owner_email, timezone,
             schedule_time, active_days, send_email,
             tone, length, format, top_insight_count, tenant_id)
        VALUES
            (:profile_id, :name, :role, :owner_email, :timezone,
             :schedule_time, CAST(:active_days AS JSONB), :send_email,
             :tone, :length, :fmt, :top_n, :tenant)
        RETURNING id
    """), {
        "profile_id":    pid,
        "name":          payload["name"],
        "role":          payload.get("role"),
        "owner_email":   payload.get("owner_email"),
        "timezone":      payload.get("timezone", "Europe/Istanbul"),
        "schedule_time": _t(payload.get("schedule_time", "06:00")),
        "active_days":   _j(payload.get("active_days", [1, 2, 3, 4, 5])),
        "send_email":    payload.get("send_email", True),
        "tone":          payload.get("tone", "yonetici"),
        "length":        payload.get("length", "ozet"),
        "fmt":           payload.get("format", "mixed"),
        "top_n":         payload.get("top_insight_count", 3),
        "tenant":        payload.get("tenant_id", _TENANT),
    })
    new_id = result.scalar()
    await session.commit()
    return {"id": new_id, "message": "Profil oluşturuldu"}


@router.put("/profiles/{profile_id}")
async def update_profile(
    profile_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _ALLOWED = {
        "name", "role", "owner_email", "timezone", "schedule_time",
        "active_days", "is_active", "send_email", "tone", "length",
        "format", "top_insight_count",
    }
    updates = {k: v for k, v in payload.items() if k in _ALLOWED}
    if not updates:
        return {"message": "Güncelleme yok"}

    parts, params = [], {"pid": profile_id}
    for key, value in updates.items():
        if key == "active_days":
            parts.append("active_days = CAST(:active_days AS JSONB)")
            params["active_days"] = _j(value)
        elif key == "schedule_time":
            parts.append("schedule_time = :schedule_time")
            params["schedule_time"] = _t(value)
        elif key == "format":
            parts.append("format = :fmt")
            params["fmt"] = value
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value

    await session.execute(
        text(f"UPDATE brief_profiles SET {', '.join(parts)}, updated_at = NOW() WHERE id = :pid"),
        params,
    )
    await session.commit()
    return {"message": "Profil güncellendi"}


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_profiles WHERE id = :pid"),
        {"pid": profile_id},
    )
    await session.commit()
    return {"message": "Profil silindi"}


# ── SORU YÖNETİMİ ─────────────────────────────────────────────────────────────

@router.get("/library/questions")
async def list_question_library(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    category: Optional[str] = None,
) -> dict:
    if category:
        rows = (await session.execute(text("""
            SELECT * FROM brief_question_library
            WHERE is_active = true AND category = :cat
            ORDER BY usage_count DESC, id
        """), {"cat": category})).mappings().all()
    else:
        rows = (await session.execute(text("""
            SELECT * FROM brief_question_library
            WHERE is_active = true
            ORDER BY category, usage_count DESC, id
        """))).mappings().all()

    grouped: Dict[str, List] = {}
    for r in rows:
        grouped.setdefault(r["category"], []).append(dict(r))

    return {"library": grouped, "total": len(rows)}


@router.post("/profiles/{profile_id}/questions")
async def add_question(
    profile_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    next_order = (await session.execute(text("""
        SELECT COALESCE(MAX(sort_order), 0) + 1
        FROM brief_questions WHERE profile_id = :pid
    """), {"pid": profile_id})).scalar() or 1

    tdays = payload.get("trigger_days")
    result = await session.execute(text("""
        INSERT INTO brief_questions
            (profile_id, question_text, agent, importance,
             is_cross_domain, trigger_days, sort_order)
        VALUES
            (:pid, :qtxt, :agent, :importance,
             :is_cross, CAST(:tdays AS JSONB), :sord)
        RETURNING id
    """), {
        "pid":       profile_id,
        "qtxt":      payload["question_text"],
        "agent":     payload["agent"],
        "importance": payload.get("importance", "orta"),
        "is_cross":  payload.get("is_cross_domain", False),
        "tdays":     _j(tdays),
        "sord":      next_order,
    })
    new_id = result.scalar()

    if payload.get("library_id"):
        await session.execute(text("""
            UPDATE brief_question_library
            SET usage_count = usage_count + 1
            WHERE id = :lid
        """), {"lid": payload["library_id"]})

    await session.commit()
    return {"id": new_id}


@router.put("/questions/{question_id}")
async def update_question(
    question_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _ALLOWED = {"question_text", "agent", "importance", "is_cross_domain",
                "trigger_days", "sort_order", "is_active"}
    updates = {k: v for k, v in payload.items() if k in _ALLOWED}
    if not updates:
        return {"message": "Güncelleme yok"}

    parts, params = [], {"qid": question_id}
    for key, value in updates.items():
        if key == "trigger_days":
            parts.append("trigger_days = CAST(:tdays AS JSONB)")
            params["tdays"] = _j(value)
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value

    await session.execute(
        text(f"UPDATE brief_questions SET {', '.join(parts)} WHERE id = :qid"),
        params,
    )
    await session.commit()
    return {"message": "Soru güncellendi"}


@router.delete("/questions/{question_id}")
async def delete_question(
    question_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_questions WHERE id = :qid"),
        {"qid": question_id},
    )
    await session.commit()
    return {"message": "Soru silindi"}


@router.post("/questions/reorder")
async def reorder_questions(
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    for order, qid in enumerate(payload.get("ordered_ids", [])):
        await session.execute(
            text("UPDATE brief_questions SET sort_order = :sord WHERE id = :qid"),
            {"sord": order, "qid": qid},
        )
    await session.commit()
    return {"message": "Sıralama kaydedildi"}


# ── KONTROL LİSTESİ ───────────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/checklist")
async def add_checklist_item(
    profile_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    next_order = (await session.execute(text("""
        SELECT COALESCE(MAX(sort_order), 0) + 1
        FROM brief_checklist_items WHERE profile_id = :pid
    """), {"pid": profile_id})).scalar() or 1

    result = await session.execute(text("""
        INSERT INTO brief_checklist_items
            (profile_id, text, priority, trigger_rule, sort_order)
        VALUES (:pid, :txt, :priority, :rule, :sord)
        RETURNING id
    """), {
        "pid":      profile_id,
        "txt":      payload["text"],
        "priority": payload.get("priority", "med"),
        "rule":     payload.get("trigger_rule"),
        "sord":     next_order,
    })
    new_id = result.scalar()
    await session.commit()
    return {"id": new_id}


@router.delete("/checklist/{item_id}")
async def delete_checklist_item(
    item_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_checklist_items WHERE id = :iid"),
        {"iid": item_id},
    )
    await session.commit()
    return {"message": "Madde silindi"}


@router.post("/checklist/toggle")
async def toggle_checklist_item(
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    today = date.today()
    is_done = bool(payload.get("is_done", False))
    done_at = datetime.utcnow() if is_done else None

    await session.execute(text("""
        INSERT INTO brief_checklist_state
            (profile_id, item_id, check_date, is_done, done_at)
        VALUES (:pid, :iid, :chk, :is_done, :done_at)
        ON CONFLICT (profile_id, item_id, check_date) DO UPDATE SET
            is_done = EXCLUDED.is_done,
            done_at = EXCLUDED.done_at
    """), {
        "pid":     payload["profile_id"],
        "iid":     payload["item_id"],
        "chk":     today,
        "is_done": is_done,
        "done_at": done_at,
    })
    await session.commit()
    return {"ok": True}


# ── BRIEF OKUMA / ÜRETME ──────────────────────────────────────────────────────

@router.get("/briefs/{profile_id}/{brief_date}")
async def get_brief(
    profile_id: int,
    brief_date: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    row = (await session.execute(text("""
        SELECT * FROM brief_history
        WHERE profile_id = :pid AND brief_date = :bdate
    """), {"pid": profile_id, "bdate": date.fromisoformat(brief_date)})).mappings().first()

    if not row:
        raise HTTPException(404, "Brief bulunamadı — önce üret")
    return dict(row)


@router.post("/briefs/{profile_id}/generate")
async def generate_brief_endpoint(
    profile_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    from app.services.daily_brief.orchestrator import generate_brief
    result = await generate_brief(profile_id, session)
    if "hata" in result:
        raise HTTPException(400, result["hata"])
    return result
