"""Validate & harden LLM-generated SQL before it touches the database.

This is layer TWO of defence (layer one is the read-only DB role). We parse the
SQL with sqlglot — a real dialect-aware parser, not regex — and enforce:

  * exactly one statement,
  * it is a SELECT (or WITH ... SELECT),
  * no DML/DDL/utility keywords anywhere in the tree,
  * a LIMIT is present (injected if missing, clamped if too high).

A rejected query raises `UnsafeSQLError` and is never executed.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.core.config import settings
from app.core.exceptions import UnsafeSQLError
from app.core.logging import get_logger

log = get_logger(__name__)

_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Command,  # COPY, GRANT, VACUUM, etc. parse as Command
)


def validate_and_harden(sql: str) -> str:
    """Return a safe, LIMIT-bounded SELECT or raise UnsafeSQLError."""
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as exc:  # noqa: BLE001 — any parse failure is unsafe
        raise UnsafeSQLError("The generated SQL could not be parsed.") from exc

    parsed = [s for s in statements if s is not None]
    if len(parsed) != 1:
        raise UnsafeSQLError("Exactly one statement is allowed.")

    root = parsed[0]

    # Top level must be a SELECT (optionally wrapped in a CTE/WITH).
    if not isinstance(root, (exp.Select, exp.Subquery, exp.Union)):
        raise UnsafeSQLError("Only SELECT statements are allowed.")

    # No forbidden node anywhere in the tree (catches CTE-hidden DML).
    for node in root.walk():
        if isinstance(node, _FORBIDDEN):
            raise UnsafeSQLError(
                f"Disallowed operation: {type(node).__name__}."
            )

    hardened = _enforce_limit(root)
    out = hardened.sql(dialect="postgres")
    log.info("sql_guard.passed", sql=out)
    return out


def _enforce_limit(select: exp.Expression) -> exp.Expression:
    """Inject a LIMIT if absent; clamp it to the configured maximum."""
    max_limit = settings.sql_max_limit
    existing = select.args.get("limit")

    if existing is None:
        return select.limit(max_limit)

    try:
        current = int(existing.expression.name)
    except (AttributeError, ValueError):
        return select.limit(max_limit)

    if current > max_limit:
        return select.limit(max_limit)
    return select
