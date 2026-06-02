"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest

# Ensure tests never reach real infra: point at throwaway values before the
# settings singleton is constructed.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("APP_ENV", "development")


@pytest.fixture
def sample_rows() -> list[dict]:
    return [
        {"month": "2026-01-01", "region": "EU", "total_revenue": 12500.0},
        {"month": "2026-02-01", "region": "EU", "total_revenue": 13800.0},
    ]
