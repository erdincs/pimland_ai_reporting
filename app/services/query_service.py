"""Orchestrates the full NL-query pipeline.

    question + filters
        -> cache lookup
        -> generate SQL        (agent, no data)
        -> validate / harden   (sql_guard)
        -> execute             (read-only session, DB does the math)
        -> summarise           (analyzer, sees only the small result)
        -> cache + return

This is the single entry point the API layer calls. Each step is an isolated,
independently testable unit — swap the LLM, the guard, or the cache without
touching the others.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.analyzer import summarise
from app.agent.text_to_sql import generate_sql
from app.core.exceptions import QueryExecutionError
from app.core.logging import get_logger
from app.schemas.query import QueryResponse
from app.services import cache_service, sql_guard

log = get_logger(__name__)


async def _execute(session: AsyncSession, sql: str) -> List[Dict[str, Any]]:
    try:
        result = await session.execute(text(sql))
        return [dict(row) for row in result.mappings().all()]
    except SQLAlchemyError as exc:
        log.warning("query.execution_failed", sql=sql, error=str(exc))
        raise QueryExecutionError() from exc


async def run_query(
    *,
    session: AsyncSession,
    question: str,
    filters: Optional[dict] = None,
    use_cache: bool = True,
) -> QueryResponse:
    started = time.perf_counter()
    cache_key = cache_service.make_key(question, filters)

    if use_cache:
        if cached := await cache_service.get(cache_key):
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            data = {k: v for k, v in cached.items() if k != "elapsed_ms"}
            return QueryResponse(**data, cached=True, elapsed_ms=elapsed)

    # 1) NL -> SQL (agent never sees data)
    sql = await generate_sql(question, filters)

    # 2) Safety: validate + harden (raises on unsafe SQL)
    safe_sql = sql_guard.validate_and_harden(sql)

    # 3) Execute on the read-only connection (DB does the aggregation)
    rows = await _execute(session, safe_sql)

    # 4) Summarise the small result set
    answer = await summarise(question, rows)

    response = QueryResponse(
        question=question,
        sql=safe_sql,
        rows=rows,
        row_count=len(rows),
        answer=answer,
        cached=False,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )

    if use_cache:
        await cache_service.set(cache_key, response.model_dump(exclude={"cached"}))

    return response
