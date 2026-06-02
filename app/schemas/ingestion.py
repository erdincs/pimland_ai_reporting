"""DTOs for the Excel ingestion endpoint."""

from __future__ import annotations

from pydantic import BaseModel


class IngestionResult(BaseModel):
    table: str
    rows_loaded: int
    columns: list[str]
    source_filename: str
    truncated_before_load: bool
