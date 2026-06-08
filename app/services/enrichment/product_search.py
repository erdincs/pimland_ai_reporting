"""Ürün arama servisi — hybrid FTS + semantic search.

product_search_index tablosunda:
  - Boş sorgu: sadece metadata filtreler → brut_ciro DESC sırala
  - FTS sorgusu: tsvector tam metin arama (hızlı)
  - Embedding varsa: cosine benzerlik (anlamsal)
  - İkisi birlikte varsa: ağırlıklı hybrid skor (0.4 FTS + 0.6 semantic)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)

_FTS_WEIGHT    = 0.4
_SEMANTIC_WEIGHT = 0.6


async def _get_query_embedding(query: str) -> Optional[List[float]]:
    """Arama sorgusunu embed et — None dönerse sadece FTS kullan."""
    try:
        from app.services.enrichment.product_indexer import _bedrock_client, _embed_sync
        loop = asyncio.get_event_loop()
        client = _bedrock_client()
        return await loop.run_in_executor(None, _embed_sync, query, client)
    except Exception as exc:
        log.warning("product_search.embed_error", error=str(exc))
        return None


def _build_where(
    marka: Optional[str],
    sezon: Optional[str],
    grade: Optional[str],
    internet_aktif: Optional[bool],
    params: Dict[str, Any],
) -> str:
    conds = ["1=1"]
    if marka:
        conds.append("marka_adi = :marka")
        params["marka"] = marka
    if sezon:
        conds.append("sezon_kodu = :sezon")
        params["sezon"] = sezon
    if grade:
        grades = [g.strip().upper() for g in grade.split(",")]
        conds.append(f"quality_grade = ANY(ARRAY{grades})")
    if internet_aktif is not None:
        conds.append("internet_aktif = :internet_aktif")
        params["internet_aktif"] = internet_aktif
    return " AND ".join(conds)


async def search_products(
    session: AsyncSession,
    q: str = "",
    marka: Optional[str] = None,
    sezon: Optional[str] = None,
    grade: Optional[str] = None,
    internet_aktif: Optional[bool] = None,
    limit: int = 24,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Ürün ara.

    Döndürür: {total, items: [{urun_kodu, urun_adi, ...score, ...}]}
    """
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    where = _build_where(marka, sezon, grade, internet_aktif, params)
    q_clean = (q or "").strip()

    # ── 1. Sorgu yok → metadata sırala ───────────────────────────────────────
    if not q_clean:
        count_row = (await session.execute(
            text(f"SELECT COUNT(*) FROM product_search_index WHERE {where}"), params
        )).scalar() or 0

        rows = (await session.execute(text(f"""
            SELECT
                urun_kodu, urun_adi, marka_adi, sezon_kodu, sezon_adi,
                tema_adi, ana_grup_adi, urun_grubu_adi, fabricmaterialname,
                default_image_url, quality_grade, quality_score,
                internet_aktif, bloke, brut_ciro
            FROM product_search_index
            WHERE {where}
            ORDER BY brut_ciro DESC, quality_score DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """), params)).mappings().all()

        return {
            "total": count_row,
            "mode": "filter",
            "items": [dict(r) for r in rows],
        }

    # ── 2. FTS sorgusu hazırla ────────────────────────────────────────────────
    params["q_fts"] = q_clean

    # ── 3. Embedding al (paralel FTS ile) ────────────────────────────────────
    embedding = await _get_query_embedding(q_clean)

    # embedding kolonunun vector tipinde olup olmadığını kontrol et
    has_vector_col = False
    try:
        type_row = (await session.execute(text("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name='product_search_index' AND column_name='embedding'
        """))).scalar()
        has_vector_col = (type_row == 'USER-DEFINED')
    except Exception:
        pass

    if embedding is not None and has_vector_col:
        # ── Hybrid search: FTS + semantic ─────────────────────────────────
        params["embedding"] = "[" + ",".join(str(v) for v in embedding) + "]"

        count_row = (await session.execute(text(f"""
            SELECT COUNT(*) FROM product_search_index
            WHERE {where}
              AND (
                fts_vector @@ plainto_tsquery('simple', :q_fts)
                OR embedding IS NOT NULL
              )
        """), params)).scalar() or 0

        rows = (await session.execute(text(f"""
            SELECT
                urun_kodu, urun_adi, marka_adi, sezon_kodu, sezon_adi,
                tema_adi, ana_grup_adi, urun_grubu_adi, fabricmaterialname,
                default_image_url, quality_grade, quality_score,
                internet_aktif, bloke, brut_ciro,
                COALESCE(ts_rank(fts_vector, plainto_tsquery('simple', :q_fts)), 0) AS fts_score,
                COALESCE(1 - (embedding <=> :embedding::vector), 0)               AS sem_score,
                (
                  {_FTS_WEIGHT} * COALESCE(ts_rank(fts_vector, plainto_tsquery('simple', :q_fts)), 0) +
                  {_SEMANTIC_WEIGHT} * COALESCE(1 - (embedding <=> :embedding::vector), 0)
                ) AS hybrid_score
            FROM product_search_index
            WHERE {where}
              AND (
                fts_vector @@ plainto_tsquery('simple', :q_fts)
                OR (embedding IS NOT NULL AND (1 - (embedding <=> :embedding::vector)) > 0.5)
              )
            ORDER BY hybrid_score DESC
            LIMIT :limit OFFSET :offset
        """), params)).mappings().all()

        mode = "hybrid"

    else:
        # ── Sadece FTS ────────────────────────────────────────────────────
        count_row = (await session.execute(text(f"""
            SELECT COUNT(*) FROM product_search_index
            WHERE {where}
              AND fts_vector @@ plainto_tsquery('simple', :q_fts)
        """), params)).scalar() or 0

        rows = (await session.execute(text(f"""
            SELECT
                urun_kodu, urun_adi, marka_adi, sezon_kodu, sezon_adi,
                tema_adi, ana_grup_adi, urun_grubu_adi, fabricmaterialname,
                default_image_url, quality_grade, quality_score,
                internet_aktif, bloke, brut_ciro,
                ts_rank(fts_vector, plainto_tsquery('simple', :q_fts)) AS fts_score,
                0 AS sem_score,
                ts_rank(fts_vector, plainto_tsquery('simple', :q_fts)) AS hybrid_score
            FROM product_search_index
            WHERE {where}
              AND fts_vector @@ plainto_tsquery('simple', :q_fts)
            ORDER BY hybrid_score DESC
            LIMIT :limit OFFSET :offset
        """), params)).mappings().all()

        mode = "fts"

    return {
        "total": count_row,
        "mode": mode,
        "items": [
            {**dict(r), "brut_ciro": float(r["brut_ciro"] or 0)}
            for r in rows
        ],
    }


async def get_index_status(session: AsyncSession) -> Dict[str, Any]:
    """Index durumu — toplam, embedding'li, son güncelleme."""
    row = (await session.execute(text("""
        SELECT
            COUNT(*) AS toplam,
            COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS embedding_li,
            COUNT(*) FILTER (WHERE fts_vector IS NOT NULL) AS fts_li,
            MAX(last_indexed_at) AS son_guncelleme
        FROM product_search_index
    """))).mappings().first()

    if not row or not row["toplam"]:
        return {"toplam": 0, "embedding_li": 0, "fts_li": 0, "son_guncelleme": None, "hazir": False}

    toplam = int(row["toplam"])
    return {
        "toplam":        toplam,
        "embedding_li":  int(row["embedding_li"]),
        "fts_li":        int(row["fts_li"]),
        "son_guncelleme": row["son_guncelleme"].isoformat() if row["son_guncelleme"] else None,
        "hazir":         toplam > 0,
    }
