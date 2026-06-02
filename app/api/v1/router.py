"""Aggregates all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import connectors, health, ingestion, portal, query, reports

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(query.router)
api_router.include_router(ingestion.router)
api_router.include_router(connectors.router)
api_router.include_router(reports.router)
api_router.include_router(portal.router)
