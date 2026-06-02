"""Request/response DTOs for the NL query endpoint."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QueryFilters(BaseModel):
    """Structured filters for the e-ticaret satış dataset."""

    yil: Optional[int] = None
    ay: Optional[int] = None
    satiskanali: Optional[str] = None
    itemcode: Optional[str] = None
    item: Optional[str] = None
    colordescription: Optional[str] = None
    itemdim1code: Optional[str] = None


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    filters: Optional[QueryFilters] = None
    use_cache: bool = True


class QueryResponse(BaseModel):
    question: str
    sql: str
    rows: List[Dict[str, Any]]
    row_count: int
    answer: str
    cached: bool = False
    elapsed_ms: Optional[float] = None
