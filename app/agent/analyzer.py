"""Result summariser: turns a small, already-aggregated result set into a
natural-language answer.

Critical cost guardrail: this receives the *summary* result set the database
computed (a handful of rows), NEVER the raw table. The query layer is
responsible for ensuring the result is small before it reaches here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.agent.llm_client import llm_client
from app.core.logging import get_logger

log = get_logger(__name__)

# Defence in depth: refuse to ship a giant result to the LLM even if an upstream
# guard failed. Keeps token cost bounded.
MAX_ROWS_TO_LLM = 200

_SYSTEM = """\
You are a data analyst for the Pimland PLM system. You are given a user's \
question and the SQL result that answers it. Write a concise, business-friendly \
answer in the SAME language as the question. State concrete numbers. Do not \
invent data beyond the result set. If the result is empty, say so plainly.\
"""


async def summarise(
    question: str,
    rows: List[Dict[str, Any]],
    *,
    language_hint: Optional[str] = None,
) -> str:
    """Produce a natural-language answer from a small result set."""
    if not rows:
        return "Bu kriterlere uygun veri bulunamadı."

    capped = rows[:MAX_ROWS_TO_LLM]
    if len(rows) > MAX_ROWS_TO_LLM:
        log.warning("analyzer.rows_truncated", total=len(rows), kept=MAX_ROWS_TO_LLM)

    user = (
        f"Question: {question}\n\n"
        f"SQL result ({len(capped)} rows):\n"
        f"{json.dumps(capped, default=str, ensure_ascii=False)}"
    )
    return await llm_client.complete(system=_SYSTEM, user=user, temperature=0.2)
