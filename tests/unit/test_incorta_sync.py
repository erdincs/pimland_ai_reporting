"""Unit tests for sync/sources/incorta_sync.py

Kritik düzeltmeleri kapsar:
- Incorta API operator büyük harf ("IN", "BETWEEN")
- Incorta API type "dimension" (integer/string değil)
- Response parsing: content.data (data değil)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sync.sources.incorta_sync import (
    _build_date_prompts,
    _build_year_month_prompts,
    _fetch_pages,
    _last_n_months,
)


# ── _build_year_month_prompts ─────────────────────────────────────────────────

def test_year_month_prompts_operator_uppercase():
    cfg = {"year_field": "CALC.Year", "month_field": "CALC.Month"}
    prompts = _build_year_month_prompts([(2026, 5)], cfg)
    ops = {p["operator"] for p in prompts}
    assert ops == {"IN"}, f"operator 'in' değil 'IN' olmalı, geldi: {ops}"


def test_year_month_prompts_type_dimension():
    cfg = {"year_field": "CALC.Year", "month_field": "CALC.Month"}
    prompts = _build_year_month_prompts([(2026, 5)], cfg)
    types = {p["type"] for p in prompts}
    assert types == {"dimension"}, f"type 'dimension' olmalı, geldi: {types}"


def test_year_month_prompts_values():
    cfg = {"year_field": "CALC.Year", "month_field": "CALC.Month"}
    prompts = _build_year_month_prompts([(2026, 4), (2026, 5), (2026, 6)], cfg)
    year_prompt = next(p for p in prompts if p["field"] == "CALC.Year")
    month_prompt = next(p for p in prompts if p["field"] == "CALC.Month")
    assert 2026 in year_prompt["values"]
    assert set(month_prompt["values"]) == {4, 5, 6}


# ── _build_date_prompts ───────────────────────────────────────────────────────

def test_date_prompts_operator_uppercase():
    cfg = {"date_field": "E_Commerce.date"}
    prompts = _build_date_prompts([(2026, 5)], cfg)
    assert prompts[0]["operator"] == "BETWEEN", \
        f"operator 'between' değil 'BETWEEN' olmalı, geldi: {prompts[0]['operator']}"


def test_date_prompts_type_dimension():
    cfg = {"date_field": "E_Commerce.date"}
    prompts = _build_date_prompts([(2026, 5)], cfg)
    assert prompts[0]["type"] == "dimension", \
        f"type 'dimension' olmalı, geldi: {prompts[0]['type']}"


def test_date_prompts_range_values():
    cfg = {"date_field": "E_Commerce.date"}
    prompts = _build_date_prompts([(2026, 5)], cfg)
    values = prompts[0]["values"]
    assert values[0] == "2026-05-01"
    assert values[1] == "2026-05-31"


# ── _fetch_pages response parsing ─────────────────────────────────────────────

def _mock_response(content_data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"content": {"data": content_data}}
    resp.raise_for_status = MagicMock()
    return resp


def test_fetch_pages_parses_content_data():
    """content.data formatından satırlar doğru okunmalı."""
    rows_page1 = [[2026, 5, "ADL", "SKU001", "Ürün", "Siyah", "M", 100.0, 1]]
    mock_resp = _mock_response({
        "headers": {"totalRows": 1},
        "data": rows_page1,
    })

    with patch("sync.sources.incorta_sync.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.post.return_value = mock_resp
        result = _fetch_pages("test_tool", "Bearer tok", [], 5000)

    assert result == rows_page1


def test_fetch_pages_empty_returns_empty_list():
    """Incorta boş data döndürünce [] beklenir, hata değil."""
    mock_resp = _mock_response({
        "headers": {"totalRows": 0},
        "data": [],
    })

    with patch("sync.sources.incorta_sync.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.post.return_value = mock_resp
        result = _fetch_pages("test_tool", "Bearer tok", [], 5000)

    assert result == []


def test_fetch_pages_pagination():
    """Toplam > pageSize ise ikinci sayfa da çekilmeli."""
    page1 = [[2026, 5, "ADL", f"SKU{i:03d}", "", "", "", 100.0, 1] for i in range(5)]
    page2 = [[2026, 5, "ADL", f"SKU{i:03d}", "", "", "", 100.0, 1] for i in range(5, 8)]

    responses = [
        _mock_response({"headers": {"totalRows": 8}, "data": page1}),
        _mock_response({"headers": {"totalRows": 8}, "data": page2}),
    ]

    with patch("sync.sources.incorta_sync.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.post.side_effect = responses
        result = _fetch_pages("test_tool", "Bearer tok", [], 5)

    assert len(result) == 8


# ── _last_n_months ────────────────────────────────────────────────────────────

def test_last_n_months_count():
    months = _last_n_months(3)
    assert len(months) == 3


def test_last_n_months_descending():
    months = _last_n_months(3)
    for i in range(len(months) - 1):
        y0, m0 = months[i]
        y1, m1 = months[i + 1]
        assert (y0, m0) > (y1, m1)
