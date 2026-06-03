"""Config dosyalarını okur, env var'ları çözümler.

Kullanım:
    from sync.config_loader import get_allowed_tools, get_field_filter, load_views

Yeni kaynak / tool eklemek için sadece YAML dosyalarını güncelle.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

_CONFIG_DIR = Path(__file__).parent / "config"


def _expand_env(obj: Any) -> Any:
    """${ENV_VAR} pattern'lerini os.environ'dan çözümle."""
    if isinstance(obj, str):
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            obj,
        )
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(i) for i in obj]
    return obj


def _load_yaml(filename: str) -> Any:
    path = _CONFIG_DIR / filename
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _expand_env(raw)


# ── Kaynak config'leri ────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_sources() -> Dict[str, Any]:
    return _load_yaml("sources.yaml")


def get_incorta_tables() -> List[Dict[str, Any]]:
    return load_sources()["incorta"]["tables"]


def get_incorta_table(name: str) -> Optional[Dict[str, Any]]:
    return next((t for t in get_incorta_tables() if t["name"] == name), None)


def get_pimland_master_tables() -> List[Dict[str, Any]]:
    return load_sources()["pimland"]["master_data"]["tables"]


def get_pimland_products_config() -> Dict[str, Any]:
    return load_sources()["pimland"]["products"]


def get_incorta_auth() -> str:
    return load_sources()["incorta"]["auth_token"]


def get_pimland_credentials() -> Dict[str, str]:
    cfg = load_sources()["pimland"]
    return {
        "token_url":     cfg["token_url"],
        "client_id":     cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "username":      cfg["username"],
        "password":      cfg["password"],
        "scope":         cfg["scope"],
    }


# ── Agent tool config'leri ────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_agent_tools_raw() -> Dict[str, Any]:
    return _load_yaml("agent_tools.yaml")


def get_allowed_tools(agent: str) -> List[str]:
    """Agent için izin verilen tool adlarını döndür."""
    cfg = _load_agent_tools_raw().get(agent, {})
    return [
        t["name"]
        for t in cfg.get("tools", [])
        if t.get("allowed", False)
    ]


def is_tool_allowed(agent: str, tool_name: str) -> bool:
    return tool_name in get_allowed_tools(agent)


def get_field_filter(agent: str, tool_name: str) -> Dict[str, List[str]]:
    """Tool'a özgü alan filtre kurallarını döndür. Yoksa boş dict."""
    cfg = _load_agent_tools_raw().get(agent, {})
    for t in cfg.get("tools", []):
        if t["name"] == tool_name and "field_filter" in t:
            return t["field_filter"]
    return {}


def get_blocked_fields(agent: str) -> Set[str]:
    """Agent'ın tüm tool'larındaki blocked alanların birleşim seti."""
    cfg = _load_agent_tools_raw().get(agent, {})
    blocked: Set[str] = set()
    for t in cfg.get("tools", []):
        for field in t.get("field_filter", {}).get("block", []):
            blocked.add(field.lower())
    return blocked


# ── View config'leri ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_views() -> List[Dict[str, Any]]:
    """View'ları order sırasına göre sıralı döndür."""
    cfg = _load_yaml("views.yaml")
    return sorted(cfg["views"], key=lambda v: v["order"])


def get_view_names() -> List[str]:
    return [v["name"] for v in load_views()]


# ── Cache temizleme (test ve reload için) ─────────────────────────────────────

def reload_config() -> None:
    """lru_cache'leri temizle — config değişikliği sonrası çağır."""
    load_sources.cache_clear()
    _load_agent_tools_raw.cache_clear()
    load_views.cache_clear()
