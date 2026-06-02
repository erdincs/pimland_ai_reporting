"""Smoke tests for the schema prompt renderer."""

from __future__ import annotations

from app.agent.schema_context import render_schema_prompt


def test_render_includes_tables_and_views():
    prompt = render_schema_prompt()
    assert "TABLE eticaret_satis" in prompt
    assert "MATERIALIZED VIEW mv_satis_aylik" in prompt
    assert "MATERIALIZED VIEW mv_satis_urun" in prompt
    assert "MATERIALIZED VIEW mv_satis_kanal" in prompt
    # Domain knowledge surfaced to the model.
    assert "satiskanali" in prompt
    assert "ciro" in prompt
    assert "pazar_payi" in prompt
