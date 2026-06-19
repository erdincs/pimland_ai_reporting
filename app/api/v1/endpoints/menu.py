"""Menü yönetim API'si — menu_agents, menu_groups, menu_items CRUD."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.core.logging import get_logger
from app.schemas.menu import MenuDataSchema, MenuSaveRequest, ToggleResponse

log = get_logger(__name__)
router = APIRouter(prefix="/menu", tags=["Menu Yönetim"])

_STATIC = Path(__file__).resolve().parent.parent.parent.parent / "static"


# ── UI ───────────────────────────────────────────────────────────────────────

@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def menu_ui() -> HTMLResponse:
    p = _STATIC / "menu-yonetim.html"
    if not p.exists():
        return HTMLResponse("<h1>menu-yonetim.html bulunamadı</h1>", status_code=404)
    return HTMLResponse(content=p.read_text(encoding="utf-8"))


# ── GET full menu ─────────────────────────────────────────────────────────────

@router.get("", response_model=MenuDataSchema)
async def get_menu(session: Annotated[AsyncSession, Depends(get_session)]) -> MenuDataSchema:
    """Tüm menü verisini döndür (agents + groups + items)."""
    agents_rows = (await session.execute(
        text("SELECT id, label, subtitle, icon, badge, badge_color, active FROM menu_agents ORDER BY sort_order")
    )).mappings().all()

    groups_rows = (await session.execute(
        text("SELECT id, label, icon, active, sort_order, nav_id FROM menu_groups ORDER BY sort_order")
    )).mappings().all()

    items_rows = (await session.execute(
        text("SELECT id, group_id, label, icon, active, sort_order, nav_id FROM menu_items ORDER BY group_id, sort_order")
    )).mappings().all()

    items_by_group: dict = {}
    for row in items_rows:
        items_by_group.setdefault(row["group_id"], []).append({
            "id": row["id"],
            "label": row["label"],
            "icon": row["icon"],
            "active": row["active"],
            "navId": row["nav_id"],
        })

    return MenuDataSchema(
        agents=[{
            "id": r["id"], "label": r["label"], "subtitle": r["subtitle"] or "",
            "icon": r["icon"] or "🤖", "badge": r["badge"] or "",
            "badgeColor": r["badge_color"] or "#ffffff", "active": r["active"],
        } for r in agents_rows],
        groups=[{
            "id": r["id"], "label": r["label"], "icon": r["icon"] or "📁",
            "active": r["active"], "order": r["sort_order"], "navId": r["nav_id"],
            "items": items_by_group.get(r["id"], []),
        } for r in groups_rows],
    )


# ── POST save (full replace) ──────────────────────────────────────────────────

@router.post("/save", response_model=MenuDataSchema)
async def save_menu(
    payload: MenuSaveRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MenuDataSchema:
    """Tüm menü durumunu kaydet — mevcut veriyi siler, yenisini ekler (transaction içinde)."""
    try:
        # items önce silinmeli (FK constraint)
        await session.execute(text("DELETE FROM menu_items"))
        await session.execute(text("DELETE FROM menu_groups"))
        await session.execute(text("DELETE FROM menu_agents"))

        for i, agent in enumerate(payload.agents):
            await session.execute(text("""
                INSERT INTO menu_agents (id, label, subtitle, icon, badge, badge_color, active, sort_order)
                VALUES (:id, :label, :subtitle, :icon, :badge, :badge_color, :active, :sort_order)
            """), {
                "id": agent.id, "label": agent.label, "subtitle": agent.subtitle,
                "icon": agent.icon, "badge": agent.badge, "badge_color": agent.badgeColor,
                "active": agent.active, "sort_order": i,
            })

        for gi, group in enumerate(payload.groups):
            await session.execute(text("""
                INSERT INTO menu_groups (id, label, icon, active, sort_order, nav_id)
                VALUES (:id, :label, :icon, :active, :sort_order, :nav_id)
            """), {
                "id": group.id, "label": group.label, "icon": group.icon,
                "active": group.active, "sort_order": gi, "nav_id": group.navId,
            })
            for ii, item in enumerate(group.items):
                await session.execute(text("""
                    INSERT INTO menu_items (id, group_id, label, icon, active, sort_order, nav_id)
                    VALUES (:id, :group_id, :label, :icon, :active, :sort_order, :nav_id)
                """), {
                    "id": item.id, "group_id": group.id, "label": item.label,
                    "icon": item.icon, "active": item.active, "sort_order": ii,
                    "nav_id": item.navId,
                })

        await session.commit()
        log.info("menu.saved", agents=len(payload.agents), groups=len(payload.groups))
    except Exception as exc:
        await session.rollback()
        log.error("menu.save_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Kayıt hatası: {exc}")

    return await get_menu(session)


# ── Granular toggle endpoints ─────────────────────────────────────────────────

@router.patch("/agents/{agent_id}/toggle", response_model=ToggleResponse)
async def toggle_agent(
    agent_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ToggleResponse:
    row = (await session.execute(
        text("SELECT active FROM menu_agents WHERE id = :id"), {"id": agent_id}
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Agent bulunamadı")
    new_val = not row["active"]
    await session.execute(
        text("UPDATE menu_agents SET active = :v, updated_at = now() WHERE id = :id"),
        {"v": new_val, "id": agent_id},
    )
    await session.commit()
    return ToggleResponse(id=agent_id, active=new_val)


@router.patch("/groups/{group_id}/toggle", response_model=ToggleResponse)
async def toggle_group(
    group_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ToggleResponse:
    row = (await session.execute(
        text("SELECT active FROM menu_groups WHERE id = :id"), {"id": group_id}
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Grup bulunamadı")
    new_val = not row["active"]
    await session.execute(
        text("UPDATE menu_groups SET active = :v, updated_at = now() WHERE id = :id"),
        {"v": new_val, "id": group_id},
    )
    await session.commit()
    return ToggleResponse(id=group_id, active=new_val)


@router.patch("/items/{item_id}/toggle", response_model=ToggleResponse)
async def toggle_item(
    item_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ToggleResponse:
    row = (await session.execute(
        text("SELECT active FROM menu_items WHERE id = :id"), {"id": item_id}
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Öğe bulunamadı")
    new_val = not row["active"]
    await session.execute(
        text("UPDATE menu_items SET active = :v, updated_at = now() WHERE id = :id"),
        {"v": new_val, "id": item_id},
    )
    await session.commit()
    return ToggleResponse(id=item_id, active=new_val)
