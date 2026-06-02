"""Connector registry — loads YAML source configs and vends connector instances.

YAML files in `config/sources/` are loaded at startup (lifespan) and on
demand via `reload()`. Adding a new data source requires only a new YAML file
and an application restart (or hot-reload via the admin API).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

import yaml

from app.connectors.base import BaseConnector
from app.connectors.mcp_client import McpConnector
from app.connectors.rest_api import RestApiConnector
from app.core.logging import get_logger
from app.schemas.connector import (
    AuthConfig,
    McpConnectionConfig,
    McpToolConfig,
    RestConnectionConfig,
    ScheduleConfig,
    SourceConfig,
)

log = get_logger(__name__)

_SOURCES_DIR = Path(__file__).parent.parent.parent / "config" / "sources"


def _resolve_env_refs(d: dict) -> dict:
    """Replace `_env: VAR_NAME` patterns with actual env values in a dict."""
    result = {}
    for k, v in d.items():
        if k.endswith("_env") and isinstance(v, str):
            result[k] = v  # keep as-is; connector reads it via os.environ
        elif isinstance(v, dict):
            result[k] = _resolve_env_refs(v)
        else:
            result[k] = v
    return result


def _parse_source_config(raw: dict) -> SourceConfig:
    src_type = raw.get("type")
    connection = None
    auth = None
    endpoints = None
    tool = None

    if src_type == "rest_api":
        conn_raw = raw.get("connection", {})
        connection = RestConnectionConfig(**conn_raw)
        if "auth" in raw:
            auth = AuthConfig(**raw["auth"])
        endpoints_raw = raw.get("endpoints", [])
        from app.schemas.connector import EndpointConfig, PaginationConfig
        endpoints = []
        for ep in endpoints_raw:
            pg_raw = ep.pop("pagination", None)
            pg = PaginationConfig(**pg_raw) if pg_raw else None
            endpoints.append(EndpointConfig(**ep, pagination=pg))

    elif src_type == "mcp":
        conn_raw = raw.get("connection", {})
        connection = McpConnectionConfig(**conn_raw)
        if "tool" in raw:
            tool = McpToolConfig(**raw["tool"])

    schedule = None
    if "schedule" in raw:
        schedule = ScheduleConfig(**raw["schedule"])

    return SourceConfig(
        source_id=raw["source_id"],
        type=src_type,
        target_table=raw["target_table"],
        description=raw.get("description", ""),
        field_map=raw.get("field_map"),
        schedule=schedule,
        connection=connection,
        auth=auth,
        endpoints=endpoints,
        tool=tool,
        enabled=raw.get("enabled", True),
    )


class ConnectorRegistry:
    def __init__(self) -> None:
        self._configs: Dict[str, SourceConfig] = {}
        self._connectors: Dict[str, BaseConnector] = {}

    def load(self, sources_dir: Path = _SOURCES_DIR) -> None:
        if not sources_dir.exists():
            log.warning("registry.sources_dir_missing", path=str(sources_dir))
            return

        loaded = 0
        for path in sorted(sources_dir.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not raw or not raw.get("enabled", True):
                    continue
                cfg = _parse_source_config(raw)
                self._configs[cfg.source_id] = cfg
                self._connectors[cfg.source_id] = self._build(cfg)
                loaded += 1
                log.info("registry.loaded", source=cfg.source_id, type=cfg.type)
            except Exception as exc:  # noqa: BLE001
                log.error("registry.load_error", file=path.name, error=str(exc))

        log.info("registry.ready", count=loaded)

    def reload(self, sources_dir: Path = _SOURCES_DIR) -> None:
        self._configs.clear()
        self._connectors.clear()
        self.load(sources_dir)

    @staticmethod
    def _build(cfg: SourceConfig) -> BaseConnector:
        if cfg.type == "rest_api":
            return RestApiConnector(cfg)
        if cfg.type == "mcp":
            return McpConnector(cfg)
        raise ValueError(f"Unknown connector type: {cfg.type}")

    def get(self, source_id: str) -> BaseConnector:
        if source_id not in self._connectors:
            raise KeyError(f"Connector '{source_id}' not found. "
                           f"Available: {list(self._connectors)}")
        return self._connectors[source_id]

    def all_configs(self) -> Dict[str, SourceConfig]:
        return dict(self._configs)

    def source_ids(self) -> list:
        return list(self._connectors)


# Module-level singleton
registry = ConnectorRegistry()
