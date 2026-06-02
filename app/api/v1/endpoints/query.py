"""Natural-language query endpoint — the Faz 1 deliverable."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session
from app.schemas.query import QueryRequest, QueryResponse
from app.services import query_service

router = APIRouter(prefix="/query", tags=["query"])


@router.post("", response_model=QueryResponse)
async def ask(
    payload: QueryRequest,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> QueryResponse:
    """Ask a question in natural language; get SQL, rows and a summary back.

    The generated SQL runs on a read-only connection and is validated by the
    SQL guard before execution.
    """
    filters = payload.filters.model_dump(exclude_none=True) if payload.filters else None
    return await query_service.run_query(
        session=session,
        question=payload.question,
        filters=filters,
        use_cache=payload.use_cache,
    )
