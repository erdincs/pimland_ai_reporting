"""Admin endpoint to ingest an uploaded Excel file into Postgres.

Faz 1: no auth yet — lock this behind the authorization layer before any
non-local deployment (see README roadmap).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

from app.ingestion.excel_loader import load_excel
from app.schemas.ingestion import IngestionResult

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post("/excel", response_model=IngestionResult)
async def ingest_excel(
    file: UploadFile = File(...),
    table: str = Form(...),
    sheet_name: str = Form("0"),
) -> IngestionResult:
    """Upload an .xlsx and load a sheet into ``table`` (full replace)."""
    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        sheet: str | int = int(sheet_name) if sheet_name.isdigit() else sheet_name
        result = load_excel(tmp_path, table=table, sheet_name=sheet)
        result.source_filename = file.filename or result.source_filename
        return result
    finally:
        tmp_path.unlink(missing_ok=True)
