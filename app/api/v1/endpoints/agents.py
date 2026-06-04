"""Agent endpoints — Call Center ve Sizewin."""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session
from app.services import callcenter_service, sizewin_service, session_store
from app.services.file_aware_agent import build_message_with_files, get_system_addendum

router = APIRouter(prefix="/agents", tags=["agents"])


# ── Call Center ───────────────────────────────────────────────────────────────

class CallCenterRequest(BaseModel):
    question: str
    urun_kodu: Optional[str] = None
    system_prompt: Optional[str] = None
    history: List[Dict[str, Any]] = []
    session_id: Optional[str] = None
    file_ids: List[str] = []


class CallCenterResponse(BaseModel):
    question: str
    answer: str
    products_found: int
    elapsed_ms: float


@router.post("/callcenter", response_model=CallCenterResponse)
async def callcenter(
    payload: CallCenterRequest,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> CallCenterResponse:
    """Call Center Agent — ürün bilgisi soruları için."""
    # Dosyaları oturumdan çek — soru STRING olarak kalır, dosyalar ayrı geçilir
    session_files = []
    if payload.session_id and payload.file_ids:
        for fid in payload.file_ids:
            f = await session_store.get_file(payload.session_id, fid)
            if f:
                session_files.append(f)

    result = await callcenter_service.run_callcenter(
        session=session,
        question=payload.question,       # her zaman string
        urun_kodu=payload.urun_kodu,
        custom_system=payload.system_prompt,
        history=payload.history,
        session_files=session_files,     # dosyalar ayrı parametre
    )
    return CallCenterResponse(**result)


# ── Sizewin ───────────────────────────────────────────────────────────────────

class Measurements(BaseModel):
    boy: Optional[float] = None    # cm
    kilo: Optional[float] = None   # kg
    gogus: Optional[float] = None  # cm
    bel: Optional[float] = None    # cm
    kalca: Optional[float] = None  # cm


class SizewinRequest(BaseModel):
    question: str
    measurements: Optional[Measurements] = None
    urun_kodu: Optional[str] = None
    urun_adi: Optional[str] = None
    system_prompt: Optional[str] = None
    history: List[Dict[str, Any]] = []
    session_id: Optional[str] = None
    file_ids: List[str] = []


class SizewinResponse(BaseModel):
    question: str
    answer: str
    product: Optional[Dict[str, Any]] = None
    size_chart_used: str
    elapsed_ms: float


@router.post("/sizewin", response_model=SizewinResponse)
async def sizewin(
    payload: SizewinRequest,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> SizewinResponse:
    """Sizewin Agent — müşteri ölçülerine göre beden önerisi."""
    measurements = payload.measurements.model_dump(exclude_none=True) if payload.measurements else None
    result = await sizewin_service.run_sizewin(
        session=session,
        question=payload.question,
        measurements=measurements,
        urun_kodu=payload.urun_kodu,
        urun_adi=payload.urun_adi,
        custom_system=payload.system_prompt,
        history=payload.history,
    )
    return SizewinResponse(**result)
