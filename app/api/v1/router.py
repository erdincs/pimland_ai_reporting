"""Aggregates all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import agents, connectors, enrichment, export, files, health, ingestion, magaza_satis, monitoring, portal, query, reports, siralama

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(query.router)
api_router.include_router(ingestion.router)
api_router.include_router(connectors.router)
api_router.include_router(reports.router)
api_router.include_router(portal.router)
api_router.include_router(agents.router)
api_router.include_router(files.router)
api_router.include_router(enrichment.router)
api_router.include_router(export.router)
api_router.include_router(monitoring.router)
api_router.include_router(siralama.router)
api_router.include_router(magaza_satis.router)
