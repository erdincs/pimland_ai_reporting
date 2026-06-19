"""Declarative base + model registry.

Import every model module here so Alembic's ``--autogenerate`` sees them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --- Model imports (extend as the schema grows) ----------------------------
from app.db.models.sync_state import SyncJob  # noqa: E402,F401
from app.db.models.menu import MenuAgent, MenuGroup, MenuItem  # noqa: E402,F401
