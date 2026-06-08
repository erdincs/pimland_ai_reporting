"""Ürün arama servisi — hybrid FTS + semantic (RRF).

Modlar:
  - filter : sorgu yok → filtrele + brut_ciro_30g DESC
  - fts    : sorgu var, vector yok → plainto_tsquery('turkish', ...)
  - hybrid : sorgu + vector → RRF (1/(k+fts_rank) + 1/(k+sem_rank)), k=60
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)

_RRF_K = 60


async def _get_query_embedding(query: str) -> Optional[List[float]]:
    try:
        from app.services.enrichment.product_indexer import _bedrock_client, _embed_sync
        loop = asyncio.get_event_loop()
        client = _bedrock_client()
        return await loop.run_in_executor(None, _embed_sync, query, client)
    except Exception as exc:
        log.warning("product_search.embed_error", error=str(exc))
        return None


async def _has_vector_col(session: AsyncSession) -> bool:
    try:
        row = (await session.execute(text("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name='product_search_index' AND column_name='embedding'
        """))).scalar()
        return row == 'USER-DEFINED'
    except Exception:
        return False


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
        # sqlalchemy text() placeholders — grade listesi parametre olarak geçemez,
        # doğrudan interpolate edilir (input ANY kontrolü üstte yapılır)
        safe_grades = ", ".join(f"'{g}'" for g in grades if g.isalpha())
        conds.append(f"quality_grade IN ({safe_grades})")
    if internet_aktif is not None:
        conds.append("internet_aktif = :internet_aktif")
        params["internet_aktif"] = internet_aktif
    return " AND ".join(conds)


_SELECT_COLS = """
    urun_kodu, urun_adi, marka_adi, sezon_kodu, sezon_adi,
    tema_adi, ana_grup_adi, urun_grubu_adi, fabricmaterialname,
    default_image_url, quality_grade, quality_score,
    internet_aktif, bloke,
    brut_ciro, brut_ciro_30g, net_ciro_30g, iade_orani
"""


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
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    where = _build_where(marka, sezon, grade, internet_aktif, params)
    q_clean = (q or "").strip()

    # ── 1. Sorgu yok → filtrele + sırala ─────────────────────────────────────
    if not q_clean:
        count_row = (await session.execute(
            text(f"SELECT COUNT(*) FROM product_search_index WHERE {where}"), params
        )).scalar() or 0

        rows = (await session.execute(text(f"""
            SELECT {_SELECT_COLS}, 0::float AS fts_score, 0::float AS sem_score,
                   0::float AS hybrid_score
            FROM product_search_index
            WHERE {where}
            ORDER BY brut_ciro_30g DESC, quality_score DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """), params)).mappings().all()

        return {"total": count_row, "mode": "filter", "items": _cast(rows)}

    # ── 2. FTS sorgusu + paralel embedding ───────────────────────────────────
    params["q_fts"] = q_clean

    embedding, use_vector = await asyncio.gather(
        _get_query_embedding(q_clean),
        _has_vector_col(session),
    )

    if embedding is not None and use_vector:
        # ── Hybrid: RRF ───────────────────────────────────────────────────
        params["emb"] = "[" + ",".join(str(v) for v in embedding) + "]"
        params["k"]   = _RRF_K

        count_row = (await session.execute(text(f"""
            SELECT COUNT(DISTINCT urun_kodu) FROM product_search_index
            WHERE {where}
              AND (
                fts_vector @@ plainto_tsquery('turkish', :q_fts)
                OR (embedding IS NOT NULL AND (1 - (embedding <=> :emb::vector)) > 0.45)
              )
        """), params)).scalar() or 0

        rows = (await session.execute(text(f"""
            WITH fts_ranked AS (
                SELECT urun_kodu,
                       ROW_NUMBER() OVER (ORDER BY ts_rank(fts_vector,
                                          plainto_tsquery('turkish', :q_fts)) DESC) AS r
                FROM product_search_index
                WHERE {where}
                  AND fts_vector @@ plainto_tsquery('turkish', :q_fts)
            ),
            sem_ranked AS (
                SELECT urun_kodu,
                       ROW_NUMBER() OVER (ORDER BY (1 - (embedding <=> :emb::vector)) DESC) AS r,
                       (1 - (embedding <=> :emb::vector)) AS sem_score
                FROM product_search_index
                WHERE {where}
                  AND embedding IS NOT NULL
                  AND (1 - (embedding <=> :emb::vector)) > 0.45
            ),
            combined AS (
                SELECT
                    COALESCE(f.urun_kodu, s.urun_kodu) AS urun_kodu,
                    COALESCE(1.0 / (:k + f.r), 0)      AS fts_rrf,
                    COALESCE(1.0 / (:k + s.r), 0)      AS sem_rrf,
                    COALESCE(s.sem_score, 0)            AS sem_score
                FROM fts_ranked f
                FULL OUTER JOIN sem_ranked s USING (urun_kodu)
            )
            SELECT p.{_SELECT_COLS.replace(chr(10),' ')},
                   COALESCE(c.fts_rrf, 0)               AS fts_score,
                   COALESCE(c.sem_score, 0)              AS sem_score,
                   (c.fts_rrf + c.sem_rrf)               AS hybrid_score
            FROM combined c
            JOIN product_search_index p USING (urun_kodu)
            ORDER BY hybrid_score DESC
            LIMIT :limit OFFSET :offset
        """), params)).mappings().all()

        return {"total": count_row, "mode": "hybrid", "items": _cast(rows)}

    # ── 3. Sadece FTS ─────────────────────────────────────────────────────────
    count_row = (await session.execute(text(f"""
        SELECT COUNT(*) FROM product_search_index
        WHERE {where}
          AND fts_vector @@ plainto_tsquery('turkish', :q_fts)
    """), params)).scalar() or 0

    rows = (await session.execute(text(f"""
        SELECT {_SELECT_COLS},
               ts_rank(fts_vector, plainto_tsquery('turkish', :q_fts)) AS fts_score,
               0::float AS sem_score,
               ts_rank(fts_vector, plainto_tsquery('turkish', :q_fts)) AS hybrid_score
        FROM product_search_index
        WHERE {where}
          AND fts_vector @@ plainto_tsquery('turkish', :q_fts)
        ORDER BY hybrid_score DESC
        LIMIT :limit OFFSET :offset
    """), params)).mappings().all()

    return {"total": count_row, "mode": "fts", "items": _cast(rows)}


def _cast(rows) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        for col in ("brut_ciro", "brut_ciro_30g", "net_ciro_30g",
                    "fts_score", "sem_score", "hybrid_score", "iade_orani"):
            if col in d and d[col] is not None:
                d[col] = float(d[col])
        out.append(d)
    return out


async def get_suggestions(
    session: AsyncSession,
    q: str,
    limit: int = 8,
) -> List[str]:
    """Autocomplete — marka + ürün adı prefix araması."""
    if len(q) < 2:
        return []
    params = {"q": f"{q}%", "limit": limit}
    rows = (await session.execute(text("""
        SELECT DISTINCT val FROM (
            SELECT marka_adi   AS val FROM product_search_index WHERE marka_adi   ILIKE :q
            UNION ALL
            SELECT urun_adi    AS val FROM product_search_index WHERE urun_adi    ILIKE :q
            UNION ALL
            SELECT tema_adi    AS val FROM product_search_index WHERE tema_adi    ILIKE :q
            UNION ALL
            SELECT ana_grup_adi AS val FROM product_search_index WHERE ana_grup_adi ILIKE :q
        ) sub
        WHERE val IS NOT NULL
        ORDER BY val
        LIMIT :limit
    """), params)).all()
    return [r[0] for r in rows]


async def get_index_status(session: AsyncSession) -> Dict[str, Any]:
    row = (await session.execute(text("""
        SELECT
            COUNT(*) AS toplam,
            COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS embedding_li,
            COUNT(*) FILTER (WHERE fts_vector IS NOT NULL) AS fts_li,
            MAX(last_indexed_at) AS son_guncelleme
        FROM product_search_index
    """))).mappings().first()

    if not row or not row["toplam"]:
        return {"toplam": 0, "embedding_li": 0, "fts_li": 0,
                "son_guncelleme": None, "hazir": False}

    toplam = int(row["toplam"])
    return {
        "toplam":        toplam,
        "embedding_li":  int(row["embedding_li"]),
        "fts_li":        int(row["fts_li"]),
        "son_guncelleme": (row["son_guncelleme"].isoformat()
                           if row["son_guncelleme"] else None),
        "hazir":         toplam > 0,
    }
