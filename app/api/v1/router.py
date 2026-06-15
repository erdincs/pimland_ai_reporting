"""Aggregates all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import agents, connectors, daily_brief, eticaret_brief, enrichment, export, files, health, ingestion, magaza_satis, monitoring, portal, query, reports, reporting_agents, siralama

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
api_router.include_router(magaza_satis.router)  # yonetici-ozeti, magaza-performans, donemseel-performans, donemseel-karsilastirma
api_router.include_router(reporting_agents.router)  # chat + insights
api_router.include_router(daily_brief.router)       # daily brief + profile mgmt
api_router.include_router(eticaret_brief.router)    # eticaret SKL-01..05 briefs
