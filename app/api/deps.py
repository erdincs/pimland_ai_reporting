"""Shared FastAPI dependencies.

Thin re-exports for now; the natural home for auth/current-user once the
multi-tenant authorization layer lands (see roadmap in README).
"""

from __future__ import annotations

from app.db.session import get_readonly_session, get_session

__all__ = ["get_session", "get_readonly_session"]
