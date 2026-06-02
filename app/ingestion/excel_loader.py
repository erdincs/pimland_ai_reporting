"""Excel -> PostgreSQL ingestion pipeline (Faz 1 first data source).

Deliberately uses the SYNC engine + pandas.to_sql: ingestion is a batch/back-
office job, not part of the request path, so the simpler sync API is the right
tool. Run it from the CLI (`scripts/load_excel.py`) or the admin endpoint.

Pipeline: read -> normalise columns -> (optional) transform -> bulk load.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from app.core.config import settings
from app.core.exceptions import IngestionError
from app.core.logging import get_logger
from app.schemas.ingestion import IngestionResult

log = get_logger(__name__)

# Lazily-created sync engine for batch loads.
_sync_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """snake_case, ascii-safe column names so they are valid SQL identifiers."""

    def clean(name: str) -> str:
        name = str(name).strip().lower()
        name = re.sub(r"[^\w]+", "_", name, flags=re.UNICODE)
        name = re.sub(r"_+", "_", name).strip("_")
        return name or "col"

    df = df.copy()
    df.columns = [clean(c) for c in df.columns]
    return df


def load_excel(
    file_path: str | Path,
    *,
    table: str,
    sheet_name: str | int = 0,
    if_exists: str = "replace",
    chunksize: int = 5_000,
) -> IngestionResult:
    """Read an .xlsx sheet and bulk-load it into ``table``.

    ``if_exists='replace'`` is the Faz 1 default (full reload). Switch to
    'append' once incremental loads / dedup keys are defined.
    """
    path = Path(file_path)
    if not path.exists():
        raise IngestionError(f"File not found: {path}")

    try:
        df = pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
    except Exception as exc:  # noqa: BLE001
        raise IngestionError(f"Could not read Excel file: {exc}") from exc

    if df.empty:
        raise IngestionError("The source sheet contains no rows.")

    df = _normalise_columns(df)

    try:
        df.to_sql(
            table,
            _sync_engine,
            if_exists=if_exists,
            index=False,
            chunksize=chunksize,
            method="multi",
        )
    except Exception as exc:  # noqa: BLE001
        raise IngestionError(f"Bulk load into {table} failed: {exc}") from exc

    log.info(
        "ingestion.loaded",
        table=table,
        rows=len(df),
        columns=list(df.columns),
        source=path.name,
    )
    return IngestionResult(
        table=table,
        rows_loaded=len(df),
        columns=list(df.columns),
        source_filename=path.name,
        truncated_before_load=(if_exists == "replace"),
    )
