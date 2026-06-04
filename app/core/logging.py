"""Structured logging — JSON in production, human-readable in dev.

Her modül:
    from app.core.logging import get_logger
    log = get_logger(__name__)

Context helper'lar:
    log_sync(log, job, tablo, eklenen, silinen, sure_sn)
    log_agent(log, agent, soru_len, mcp_ms, yanit_ms, guard_hit)
    log_guard(log, katman, neden, oturum_id, agent)
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

import structlog

from app.core.config import settings


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        # JSON — CloudWatch'a direkt parse edilebilir
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


# ── Structured context helper'lar ─────────────────────────────────────────────

def log_sync(
    log: structlog.stdlib.BoundLogger,
    job_name: str,
    tablo: str,
    eklenen: int = 0,
    silinen: int = 0,
    sure_sn: float = 0.0,
    hata: Optional[str] = None,
) -> None:
    """Sync job sonucunu yapılandırılmış formatta logla."""
    kwargs = dict(
        service="sync",
        job_name=job_name,
        tablo=tablo,
        eklenen=eklenen,
        silinen=silinen,
        duration_ms=round(sure_sn * 1000),
    )
    if hata:
        log.error("sync.job_failed", **kwargs, hata=hata)
    else:
        log.info("sync.job_done", **kwargs)


def log_agent(
    log: structlog.stdlib.BoundLogger,
    agent_name: str,
    soru_uzunlugu: int,
    mcp_ms: Optional[int] = None,
    yanit_ms: Optional[int] = None,
    guard_hit: bool = False,
    mcp_timeout: bool = False,
) -> None:
    """Agent çağrısını yapılandırılmış formatta logla."""
    log.info(
        "agent.request",
        service="agent",
        agent_name=agent_name,
        soru_uzunlugu=soru_uzunlugu,
        mcp_ms=mcp_ms,
        yanit_ms=yanit_ms,
        guard_hit=guard_hit,
        mcp_timeout=mcp_timeout,
    )


def log_guard(
    log: structlog.stdlib.BoundLogger,
    katman: str,
    neden: str,
    oturum_id: str,
    agent: str = "",
    seviye: str = "warning",
) -> None:
    """Guard tetiklenmesini yapılandırılmış formatta logla."""
    fn = getattr(log, seviye, log.warning)
    fn(
        "guard.triggered",
        service="guard",
        katman=katman,
        neden=neden,
        oturum_id=oturum_id,
        agent=agent,
    )
