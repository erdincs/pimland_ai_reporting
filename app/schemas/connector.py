"""Pydantic schemas for connector configuration and sync job state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ── Source configuration (mirrors the YAML structure) ───────────────────────

class AuthConfig(BaseModel):
    type: Literal[
        "api_key", "bearer",
        "oauth2_client_credentials",
        "oauth2_password_grant",   # ROPC — username+password
        "none"
    ] = "none"
    # env-var names — actual secrets never stored here
    token_env: Optional[str] = None
    key_header: str = "Authorization"
    key_prefix: str = "Bearer"
    # oauth2 shared fields
    token_url_env: Optional[str] = None
    client_id_env: Optional[str] = None
    client_secret_env: Optional[str] = None
    scope: Optional[str] = None        # literal scope string
    scope_env: Optional[str] = None    # or read from env var
    # oauth2_password_grant only
    username_env: Optional[str] = None
    password_env: Optional[str] = None


class PaginationConfig(BaseModel):
    strategy: Literal["none", "page_number", "cursor", "link_header", "offset_limit"] = "none"
    page_param: str = "page"
    page_size_param: str = "per_page"
    page_size: int = 100
    total_pages_field: Optional[str] = None   # dot-notation
    cursor_field: Optional[str] = None        # field in response containing next cursor
    cursor_param: Optional[str] = None        # query param name for cursor


class EndpointConfig(BaseModel):
    path: str
    method: str = "GET"
    params: Dict[str, Any] = Field(default_factory=dict)
    response_data_path: Optional[str] = None  # dot-notation: "data.items"
    pagination: Optional[PaginationConfig] = None


class RestConnectionConfig(BaseModel):
    base_url: str
    timeout_seconds: int = 30
    requests_per_second: Optional[float] = None


class McpConnectionConfig(BaseModel):
    url: str                           # HTTP/SSE transport URL
    timeout_seconds: int = 60
    retry_attempts: int = 3
    retry_backoff_seconds: float = 2.0


class McpToolConfig(BaseModel):
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    response_data_path: Optional[str] = None
    # Incorta columnar format: headers+data arrays → dict list
    response_format: Optional[str] = None        # "incorta" for columnar responses
    # Pagination for tools that support startRow/pageSize
    pagination_start_field: Optional[str] = None  # e.g. "pagination.startRow"
    pagination_size_field: Optional[str] = None   # e.g. "pagination.pageSize"
    pagination_page_size: int = 5000
    total_rows_path: Optional[str] = None         # dot-path to totalRows in response
    # Env var placeholders in args: {"Authorization": "__env:INCORTA_TOKEN"}
    # resolved at runtime so secrets stay out of YAML


class ScheduleConfig(BaseModel):
    cron: Optional[str] = None              # "0 3 * * *"
    interval_minutes: Optional[int] = None  # mutually exclusive with cron


class SourceConfig(BaseModel):
    source_id: str
    type: Literal["rest_api", "mcp"]
    target_table: str
    description: str = ""
    field_map: Optional[Dict[str, str]] = None
    schedule: Optional[ScheduleConfig] = None
    # type-specific (one will be None)
    connection: Optional[Any] = None   # RestConnectionConfig | McpConnectionConfig
    auth: Optional[AuthConfig] = None
    endpoints: Optional[List[EndpointConfig]] = None
    tool: Optional[McpToolConfig] = None
    enabled: bool = True


# ── Sync job state ──────────────────────────────────────────────────────────

class SyncJobStatus(str):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SyncJobResult(BaseModel):
    job_id: str
    source_id: str
    status: str
    rows_loaded: Optional[int] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[float] = None


class SourceListItem(BaseModel):
    source_id: str
    type: str
    target_table: str
    description: str
    enabled: bool
    last_sync: Optional[SyncJobResult] = None
