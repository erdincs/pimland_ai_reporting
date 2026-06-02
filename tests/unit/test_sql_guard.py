"""Unit tests for the SQL safety guard — the security-critical component."""

from __future__ import annotations

import pytest

from app.core.exceptions import UnsafeSQLError
from app.services.sql_guard import validate_and_harden


def test_plain_select_passes_and_gets_limit():
    out = validate_and_harden("SELECT product_code FROM sales")
    assert out.lower().startswith("select")
    assert "limit" in out.lower()


def test_existing_limit_preserved_when_within_cap():
    out = validate_and_harden("SELECT * FROM sales LIMIT 5")
    assert "limit 5" in out.lower()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM sales",
        "UPDATE sales SET revenue = 0",
        "DROP TABLE sales",
        "INSERT INTO sales VALUES (1)",
        "TRUNCATE sales",
        "SELECT 1; DROP TABLE sales",  # statement stacking
        "GRANT ALL ON sales TO public",
    ],
)
def test_dangerous_statements_rejected(sql: str):
    with pytest.raises(UnsafeSQLError):
        validate_and_harden(sql)


def test_cte_hidden_dml_rejected():
    sql = "WITH x AS (DELETE FROM sales RETURNING *) SELECT * FROM x"
    with pytest.raises(UnsafeSQLError):
        validate_and_harden(sql)


def test_garbage_rejected():
    with pytest.raises(UnsafeSQLError):
        validate_and_harden("not sql at all ;;;")
