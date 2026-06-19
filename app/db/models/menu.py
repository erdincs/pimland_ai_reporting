"""ORM models for menu management."""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class MenuAgent(Base, TimestampMixin):
    __tablename__ = "menu_agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(200), server_default="")
    icon: Mapped[str] = mapped_column(String(16), server_default="🤖")
    badge: Mapped[str] = mapped_column(String(16), server_default="")
    badge_color: Mapped[str] = mapped_column(String(32), server_default="#ffffff")
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")


class MenuGroup(Base, TimestampMixin):
    __tablename__ = "menu_groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    icon: Mapped[str] = mapped_column(String(16), server_default="📁")
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")


class MenuItem(Base, TimestampMixin):
    __tablename__ = "menu_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    icon: Mapped[str] = mapped_column(String(16), server_default="📄")
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
