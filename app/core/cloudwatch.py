"""CloudWatch log writer.

CLOUDWATCH_ENABLED=true  → boto3 ile CloudWatch Logs'a yazar
CLOUDWATCH_ENABLED=false → sadece stdout (geliştirme ortamı)

Log group'lar:
  /pimland/sync/incorta
  /pimland/sync/pimland-mcp
  /pimland/sync/views
  /pimland/agent/callcenter
  /pimland/agent/sizewin
  /pimland/guard
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("CLOUDWATCH_ENABLED", "false").lower() == "true"
_REGION  = os.environ.get("AWS_REGION", "eu-north-1")

# Log group → stream önbelleği {group: stream_name}
_STREAM_CACHE: Dict[str, str] = {}
_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("logs", region_name=_REGION)
    return _client


def _ensure_group_and_stream(group: str) -> str:
    """Log group ve stream'i oluştur (yoksa). Stream adı = tarih."""
    if group in _STREAM_CACHE:
        return _STREAM_CACHE[group]

    client = _get_client()
    stream = time.strftime("%Y/%m/%d")

    try:
        client.create_log_group(logGroupName=group)
    except client.exceptions.ResourceAlreadyExistsException:
        pass
    except Exception as e:
        log.warning("cloudwatch.create_group_failed group=%s err=%s", group, e)
        return stream

    try:
        client.create_log_stream(logGroupName=group, logStreamName=stream)
    except client.exceptions.ResourceAlreadyExistsException:
        pass
    except Exception as e:
        log.warning("cloudwatch.create_stream_failed group=%s err=%s", group, e)

    _STREAM_CACHE[group] = stream
    return stream


def send(group: str, event: Dict[str, Any]) -> None:
    """Tek bir log event'i CloudWatch'a gönder."""
    if not _ENABLED:
        return
    try:
        client  = _get_client()
        stream  = _ensure_group_and_stream(group)
        message = json.dumps(event, ensure_ascii=False, default=str)
        client.put_log_events(
            logGroupName=group,
            logStreamName=stream,
            logEvents=[{"timestamp": int(time.time() * 1000), "message": message}],
        )
    except Exception as e:
        log.warning("cloudwatch.send_failed group=%s err=%s", group, e)


# ── Yardımcı göndericiler ─────────────────────────────────────────────────────

LOG_GROUPS = {
    "sync_incorta":   "/pimland/sync/incorta",
    "sync_pimland":   "/pimland/sync/pimland-mcp",
    "sync_views":     "/pimland/sync/views",
    "agent_cc":       "/pimland/agent/callcenter",
    "agent_sw":       "/pimland/agent/sizewin",
    "guard":          "/pimland/guard",
}


def log_sync_event(job: str, tablo: str, eklenen: int, silinen: int,
                   sure_ms: int, status: str = "success", hata: str = "") -> None:
    group_key = "sync_pimland" if "pimland" in job else "sync_incorta"
    if "view" in job:
        group_key = "sync_views"
    send(LOG_GROUPS[group_key], {
        "event": "sync_job", "job": job, "tablo": tablo,
        "eklenen": eklenen, "silinen": silinen,
        "duration_ms": sure_ms, "status": status, "hata": hata,
    })


def log_agent_event(agent: str, soru_len: int, mcp_ms: Optional[int],
                    yanit_ms: Optional[int], guard_hit: bool, mcp_timeout: bool = False) -> None:
    group_key = "agent_cc" if agent == "callcenter" else "agent_sw"
    send(LOG_GROUPS[group_key], {
        "event": "agent_request", "agent": agent,
        "soru_uzunlugu": soru_len, "mcp_ms": mcp_ms,
        "yanit_ms": yanit_ms, "guard_hit": guard_hit,
        "mcp_timeout": mcp_timeout,
    })


def log_guard_event(katman: str, neden: str, oturum_id: str, agent: str = "") -> None:
    send(LOG_GROUPS["guard"], {
        "event": "guard_triggered", "katman": katman,
        "neden": neden, "oturum_id": oturum_id, "agent": agent,
    })
