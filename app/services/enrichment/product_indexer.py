"""Ürün arama indexleyici — pim_products → product_search_index.

Her ürün için:
  1. search_text belgesi oluşturur (ad + marka + sezon + tema + kategori + kumaş)
  2. AWS Bedrock Titan Embed v2 ile 1024 boyutlu embedding üretir
  3. product_search_index tablosuna UPSERT yapar
  4. fts_vector kolonunu SQL ile günceller

Nightly job (04:00) + manuel tetikleme (POST /enrichment/search/index).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import boto3
import psycopg2
import psycopg2.extras

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
_EMBED_DIMS  = 1024
_BATCH_CONCURRENT = 8   # eş zamanlı Bedrock çağrısı
_UPSERT_CHUNK = 200     # tek seferde UPSERT edilen satır sayısı


# ── Bedrock embedding ─────────────────────────────────────────────────────────

def _bedrock_client():
    kwargs: Dict[str, Any] = {"region_name": settings.bedrock_region}
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"]     = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("bedrock-runtime", **kwargs)


def _embed_sync(text: str, client) -> Optional[List[float]]:
    """Tek metni embed et (senkron — executor içinde çalışır)."""
    try:
        body = json.dumps({
            "inputText": text[:8000],
            "dimensions": _EMBED_DIMS,
            "normalize": True,
        })
        resp = client.invoke_model(
            modelId=_EMBED_MODEL,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(resp["body"].read())["embedding"]
    except Exception as exc:
        log.warning("indexer.embed_error", error=str(exc))
        return None


async def _embed_async(text: str, client, sem: asyncio.Semaphore) -> Optional[List[float]]:
    loop = asyncio.get_event_loop()
    async with sem:
        return await loop.run_in_executor(None, _embed_sync, text, client)


# ── Search text builder ───────────────────────────────────────────────────────

def _build_search_text(row: Dict[str, Any]) -> str:
    parts = [
        row.get("urun_adi") or "",
        row.get("marka_adi") or "",
        row.get("sezon_adi") or "",
        row.get("sezon_kodu") or "",
        row.get("tema_adi") or "",
        row.get("ana_grup_adi") or "",
        row.get("urun_grubu_adi") or "",
        row.get("fabricmaterialname") or "",
    ]
    return " ".join(p for p in parts if p).strip()


# ── DB helpers ────────────────────────────────────────────────────────────────

def _fetch_products(conn) -> List[Dict[str, Any]]:
    """pim_products + enrichment_quality + satış toplamı çek."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                p.urun_kodu,
                p.urun_adi,
                p.marka_adi,
                p.sezon_kodu,
                p.sezon_adi,
                p.tema_adi,
                p.ana_grup_adi,
                p.urun_grubu_adi,
                p.fabricmaterialname,
                p.color_codes,
                p.default_image_url,
                p.internet_aktif,
                p.bloke,
                eq.quality_grade,
                eq.quality_score,
                COALESCE(s.brut_ciro, 0) AS brut_ciro
            FROM pim_products p
            LEFT JOIN enrichment_quality eq ON eq.urun_kodu = p.urun_kodu
            LEFT JOIN (
                SELECT urun_kodu, SUM(tutar) AS brut_ciro
                FROM incorta_satis
                WHERE yil IN (2025, 2026)
                GROUP BY urun_kodu
            ) s ON s.urun_kodu = p.urun_kodu
            ORDER BY p.urun_kodu
        """)
        return [dict(r) for r in cur.fetchall()]


def _has_vector_type(conn) -> bool:
    """embedding kolonunun vector (pgvector) tipinde olup olmadığını kontrol et."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name='product_search_index' AND column_name='embedding'
        """)
        row = cur.fetchone()
        return bool(row and row[0] == 'USER-DEFINED')


def _format_embedding(embedding: Optional[List[float]], use_vector: bool) -> Optional[str]:
    """Embedding'i DB kolonuna uygun formata çevir."""
    if embedding is None:
        return None
    if use_vector:
        # pgvector expects '[x,y,z]' string format
        return "[" + ",".join(str(v) for v in embedding) + "]"
    # TEXT kolonu — JSON array string
    return json.dumps(embedding)


def _upsert_batch(conn, rows: List[Dict[str, Any]], use_vector: bool) -> None:
    """Bir batch ürünü UPSERT et (embedding dahil)."""
    emb_cast = "%s::vector" if use_vector else "%s"
    template = f"(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,{emb_cast},NOW())"

    with conn.cursor() as cur:
        values = [
            (
                r["urun_kodu"], r["urun_adi"], r["marka_adi"],
                r["sezon_kodu"], r["sezon_adi"], r["tema_adi"],
                r["ana_grup_adi"], r["urun_grubu_adi"],
                r["fabricmaterialname"], r["color_codes"],
                r["default_image_url"],
                r["quality_grade"], r["quality_score"],
                r["internet_aktif"], r["bloke"],
                float(r["brut_ciro"]) if r["brut_ciro"] else 0.0,
                r["search_text"],
                _format_embedding(r["embedding"], use_vector),
            )
            for r in rows
        ]
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO product_search_index (
                urun_kodu, urun_adi, marka_adi,
                sezon_kodu, sezon_adi, tema_adi,
                ana_grup_adi, urun_grubu_adi,
                fabricmaterialname, color_codes,
                default_image_url,
                quality_grade, quality_score,
                internet_aktif, bloke,
                brut_ciro,
                search_text,
                embedding,
                last_indexed_at
            ) VALUES %s
            ON CONFLICT (urun_kodu) DO UPDATE SET
                urun_adi           = EXCLUDED.urun_adi,
                marka_adi          = EXCLUDED.marka_adi,
                sezon_kodu         = EXCLUDED.sezon_kodu,
                sezon_adi          = EXCLUDED.sezon_adi,
                tema_adi           = EXCLUDED.tema_adi,
                ana_grup_adi       = EXCLUDED.ana_grup_adi,
                urun_grubu_adi     = EXCLUDED.urun_grubu_adi,
                fabricmaterialname = EXCLUDED.fabricmaterialname,
                color_codes        = EXCLUDED.color_codes,
                default_image_url  = EXCLUDED.default_image_url,
                quality_grade      = EXCLUDED.quality_grade,
                quality_score      = EXCLUDED.quality_score,
                internet_aktif     = EXCLUDED.internet_aktif,
                bloke              = EXCLUDED.bloke,
                brut_ciro          = EXCLUDED.brut_ciro,
                search_text        = EXCLUDED.search_text,
                embedding          = EXCLUDED.embedding,
                last_indexed_at    = NOW()
            """,
            values,
            template=template,
        )
        conn.commit()


def _update_fts(conn) -> None:
    """tsvector kolonunu search_text'ten güncelle."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE product_search_index
            SET fts_vector = to_tsvector('simple', COALESCE(search_text,''))
            WHERE fts_vector IS NULL
               OR last_indexed_at > NOW() - INTERVAL '10 minutes'
        """)
        conn.commit()


def _create_ivfflat_index_if_needed(conn) -> None:
    """ivfflat index — en az 100 satır gerektiriyor, ilk indexlemeden sonra oluştur."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM product_search_index WHERE embedding IS NOT NULL")
        count = cur.fetchone()[0]
        if count < 100:
            return
        cur.execute("""
            SELECT 1 FROM pg_indexes
            WHERE tablename='product_search_index' AND indexname='idx_psi_embedding'
        """)
        if cur.fetchone():
            return
        log.info("indexer.creating_ivfflat_index", rows=count)
        cur.execute("""
            CREATE INDEX idx_psi_embedding
            ON product_search_index
            USING ivfflat(embedding vector_cosine_ops)
            WITH (lists = 100)
        """)
        conn.commit()


# ── Ana indexer ───────────────────────────────────────────────────────────────

async def run_indexer(
    progress_cb=None,
    skip_embeddings: bool = False,
) -> Dict[str, Any]:
    """
    Tüm ürünleri indexle.

    progress_cb(current, total) — opsiyonel ilerleme callback'i.
    skip_embeddings=True — embedding olmadan sadece FTS indexler (hızlı, test için).
    """
    t0 = time.perf_counter()
    log.info("indexer.started")

    conn = psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        products = _fetch_products(conn)
        total = len(products)
        log.info("indexer.products_fetched", count=total)

        if not products:
            return {"status": "done", "indexed": 0, "elapsed_ms": 0, "errors": 0}

        use_vector = _has_vector_type(conn)
        log.info("indexer.embedding_mode", use_vector=use_vector)

        if not skip_embeddings and use_vector:
            bedrock = _bedrock_client()
            sem = asyncio.Semaphore(_BATCH_CONCURRENT)

        errors = 0
        processed = 0

        # Embedding + search_text üret
        for i in range(0, total, _UPSERT_CHUNK):
            chunk = products[i: i + _UPSERT_CHUNK]

            # Search text her zaman üret
            for r in chunk:
                r["search_text"] = _build_search_text(r)
                r["embedding"] = None

            if not skip_embeddings and use_vector:
                embeddings = await asyncio.gather(*[
                    _embed_async(r["search_text"], bedrock, sem) for r in chunk
                ])
                for r, emb in zip(chunk, embeddings):
                    r["embedding"] = emb
                    if emb is None:
                        errors += 1

            _upsert_batch(conn, chunk, use_vector)
            processed += len(chunk)

            if progress_cb:
                progress_cb(processed, total)

            log.info("indexer.chunk_done", processed=processed, total=total)

        _update_fts(conn)
        if not skip_embeddings:
            _create_ivfflat_index_if_needed(conn)

        elapsed = round((time.perf_counter() - t0) * 1000)
        log.info("indexer.completed", indexed=processed, errors=errors, elapsed_ms=elapsed)

        return {
            "status": "done",
            "indexed": processed,
            "errors": errors,
            "elapsed_ms": elapsed,
        }

    except Exception as exc:
        log.error("indexer.failed", error=str(exc))
        raise
    finally:
        conn.close()
