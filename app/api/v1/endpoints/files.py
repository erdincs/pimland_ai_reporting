"""Dosya yükleme ve yönetim endpoint'leri."""

from __future__ import annotations

from typing import Annotated, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.core.config import settings
from app.services import file_processor, session_store

router = APIRouter(prefix="/files", tags=["files"])


@router.post("/upload")
async def upload_file(
    file:       UploadFile = File(...),
    session_id: str = Form(...),
    db:         AsyncSession = Depends(get_session),
):
    """Dosyayı işle ve oturum hafızasına kaydet."""
    data         = await file.read()
    content_type = file.content_type or ""
    filename     = file.filename or "dosya"

    try:
        meta = file_processor.process_file(
            data        = data,
            filename    = filename,
            content_type= content_type,
            session_id  = session_id,
            db_url      = settings.database_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dosya işlenemedi: {e}")

    file_id = await session_store.store_file(session_id, meta)

    # Frontend'e dönen özet (base64 hariç — büyük)
    summary = {
        "file_id":  file_id,
        "filename": meta["filename"],
        "type":     meta["type"],
    }
    if meta["type"] == "document":
        summary["chars"]     = len(meta.get("text", ""))
        summary["truncated"] = meta.get("truncated", False)
        if meta.get("pages"):
            summary["pages"] = meta["pages"]
    elif meta["type"] == "dataframe":
        summary["tables"] = [
            {
                "sheet":    t["sheet"],
                "pg_table": t["pg_table"],
                "rows":     t["rows"],
                "columns":  t["columns"],
                "preview":  t["preview"],
            }
            for t in meta["tables"]
        ]
    elif meta["type"] == "image":
        summary["mime"] = meta.get("mime")

    return summary


@router.delete("/{file_id}")
async def delete_file(
    file_id:    str,
    session_id: str,
    db:         AsyncSession = Depends(get_session),
):
    """Dosyayı ve varsa geçici PostgreSQL tablosunu sil."""
    deleted = await session_store.delete_file(session_id, file_id, db_session=db)
    if not deleted:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    return {"deleted": file_id}


@router.get("/session/{session_id}")
async def list_session_files(session_id: str) -> List[dict]:
    """Oturumdaki tüm dosyaları listele."""
    files = await session_store.get_session_files(session_id)
    # base64 veriyi listede gösterme
    for f in files:
        f.pop("base64", None)
        f.pop("text", None)
    return files


@router.delete("/session/{session_id}")
async def cleanup_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Oturumu ve tüm dosyaları temizle."""
    count = await session_store.cleanup_session(session_id, db_session=db)
    return {"cleaned": count}
