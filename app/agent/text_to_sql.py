"""Text-to-SQL agent: natural language question -> raw SQL string.

This module ONLY produces SQL. It does not execute it and never sees result
rows — that separation is core to the cost/safety architecture. Validation and
execution happen downstream in `services.sql_guard` and `services.query_service`.
"""

from __future__ import annotations

import re

from app.agent.llm_client import llm_client
from app.agent.prompts.text_to_sql import (
    SYSTEM_TEMPLATE,
    USER_TEMPLATE,
    build_filters_block,
)
from app.agent.schema_context import render_schema_prompt
from app.core.config import settings
from app.core.exceptions import SQLGenerationError
from app.core.logging import get_logger

log = get_logger(__name__)

_SQL_BLOCK = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_sql(raw: str) -> str:
    match = _SQL_BLOCK.search(raw)
    sql = (match.group(1) if match else raw).strip().rstrip(";").strip()
    if not sql:
        raise SQLGenerationError("The model returned no SQL.")
    return sql


async def generate_sql(question: str, filters: "Optional[dict]" = None) -> str:
    """Translate a NL question (+ optional structured filters) into SQL."""
    system = SYSTEM_TEMPLATE.format(
        schema=render_schema_prompt(),
        max_limit=settings.sql_max_limit,
    )
    user = USER_TEMPLATE.format(
        question=question.strip(),
        filters_block=build_filters_block(filters),
    )

    raw = await llm_client.complete(system=system, user=user)
    sql = _extract_sql(raw)
    log.info("agent.sql_generated", question=question, sql=sql)
    return sql
