"""Ürün arama indexleyici — pim_products + enrichment_quality → product_search_index.

Her ürün için:
  1. search_text belgesi oluşturur (temel + zengin MCP verisi detail_json'dan)
  2. pimland_hash hesaplar → delta indexing (sadece değişenler)
  3. AWS Bedrock Titan Embed v2 ile embedding üretir (pgvector mevcut ise)
  4. UPSERT yapar — fts_vector GENERATED ALWAYS AS ile otomatik güncellenir

Nightly job (04:00) + manuel tetikleme (POST /enrichment/search/index).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Dict, List, Optional

import boto3
import psycopg2
import psycopg2.extras

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_EMBED_MODEL      = "amazon.titan-embed-text-v2:0"
_EMBED_DIMS       = 1024
_BATCH_CONCURRENT = 8
_UPSERT_CHUNK     = 200


# ── Bedrock embedding ─────────────────────────────────────────────────────────

def _bedrock_client():
    kwargs: Dict[str, Any] = {"region_name": settings.bedrock_region}
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"]     = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("bedrock-runtime", **kwargs)


def _embed_sync(text: str, client) -> Optional[List[float]]:
    try:
        body = json.dumps({
            "inputText": text[:8000],
            "dimensions": _EMBED_DIMS,
            "normalize": True,
        })
        resp = client.invoke_model(
            modelId=_EMBED_MODEL, body=body,
            contentType="application/json", accept="application/json",
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
    """
    Hem pim_products hem detail_json'dan zengin arama belgesi oluştur.

    detail_json iki yapıda gelebilir:
      - attrs bloğu varsa  → yeni scorer çıktısı (ham attribute'lar)
      - attrs bloğu yoksa  → eski nested yapı (kumas/satis/temel/gorsel)
    Her iki durumda da doğru değerleri çeker.
    """
    parts: List[str] = []

    # ── Temel kimlik (pim_products) ──────────────────────────────────────────
    if row.get("marka_adi"):      parts.append(row["marka_adi"])
    if row.get("urun_adi"):       parts.append(row["urun_adi"])
    if row.get("sezon_adi"):      parts.append(row["sezon_adi"])
    if row.get("tema_adi"):       parts.append(f"Tema: {row['tema_adi']}")
    if row.get("ana_grup_adi"):   parts.append(row["ana_grup_adi"])
    if row.get("urun_grubu_adi"): parts.append(row["urun_grubu_adi"])
    if row.get("fabricmaterialname"):
        parts.append(f"Kumaş: {row['fabricmaterialname']}")

    detail: Dict = row.get("detail_json") or {}

    # ── Yeni yapı: attrs bloğu ───────────────────────────────────────────────
    attrs: Dict = detail.get("attrs") or {}

    def _a(key: str) -> str:
        return (attrs.get(key) or "").strip()

    if _a("description"):        parts.append(_a("description")[:300])
    if _a("fabricMaterialName"): parts.append(f"Kumaş: {_a('fabricMaterialName')}")
    if _a("mainMaterialContent"):parts.append(f"İçerik: {_a('mainMaterialContent')[:150]}")
    if _a("fitName"):            parts.append(f"Kalıp: {_a('fitName')}")
    if _a("productTypeName"):    parts.append(_a("productTypeName"))

    # Ürün özellikleri (yeni alanlar)
    if _a("styleName"):          parts.append(f"Stil: {_a('styleName')}")
    if _a("armLengthName"):      parts.append(_a("armLengthName"))
    if _a("collarTypeName"):     parts.append(_a("collarTypeName"))
    if _a("fabricPatternName"):  parts.append(_a("fabricPatternName"))
    if _a("productLengthName"):  parts.append(_a("productLengthName"))

    # E-ticaret etiketleri
    for i in range(1, 5):
        tag = attrs.get(f"ecomTag{i}")
        if tag: parts.append(str(tag))

    # Renk adları
    for renk in (attrs.get("renk_adlari") or [])[:5]:
        if renk: parts.append(renk)

    # Ürün hikayeleri
    for s in (attrs.get("productStories") or [])[:2]:
        if s: parts.append(str(s)[:200])

    if _a("notes"): parts.append(f"Not: {_a('notes')[:200]}")

    # Bakım talimatları
    care = [c for c in (attrs.get("washingAndCareInstructions") or []) if c]
    if care: parts.append("Bakım: " + ", ".join(care[:4]))

    # ── Eski yapı: nested detail (attrs yoksa) ───────────────────────────────
    if not attrs:
        def _deger(section: str, key: str) -> str:
            val = ((detail.get(section) or {}).get(key) or {}).get("deger")
            return str(val).strip() if val and val != "None" else ""

        if _deger("satis", "Ürün Açıklaması"):
            parts.append(_deger("satis", "Ürün Açıklaması")[:300])
        if _deger("kumas", "Kumaş Adı"):
            parts.append(f"Kumaş: {_deger('kumas', 'Kumaş Adı')}")
        if _deger("kumas", "Kumaş İçerik %"):
            parts.append(f"İçerik: {_deger('kumas', 'Kumaş İçerik %')[:150]}")
        if _deger("kumas", "Kalıp Tipi"):
            parts.append(f"Kalıp: {_deger('kumas', 'Kalıp Tipi')}")
        if _deger("temel", "Kumaş Tipi"):
            parts.append(_deger("temel", "Kumaş Tipi"))
        if _deger("satis", "Koleksiyon Teması"):
            parts.append(_deger("satis", "Koleksiyon Teması"))
        for i in range(1, 5):
            tag = _deger("satis", f"E-Ticaret Etiketi {i}")
            if tag: parts.append(tag)
        care_val = ((detail.get("kumas") or {}).get("Bakım Talimatları") or {}).get("deger")
        if isinstance(care_val, list):
            parts.append("Bakım: " + ", ".join(str(c) for c in care_val[:4] if c))

    return " | ".join(p for p in parts if p).strip()


def _compute_hash(row: Dict[str, Any]) -> str:
    """Delta indexing için kararlı hash — anahtar alanlar değişince farklı döner."""
    detail: Dict = row.get("detail_json") or {}
    key = {
        "urun_adi":    row.get("urun_adi"),
        "marka_adi":   row.get("marka_adi"),
        "sezon_kodu":  row.get("sezon_kodu"),
        "tema_adi":    row.get("tema_adi"),
        "ana_grup":    row.get("ana_grup_adi"),
        "kumaş":       row.get("fabricmaterialname"),
        "description": detail.get("description"),
        "fabricMat":   detail.get("fabricMaterialName"),
        "fit":         detail.get("fitName"),
        "stories":     [s.get("storyText") for s in (detail.get("productStories") or [])[:2]],
        "ecomTag1":    detail.get("ecomTag1"),
        "ecomTag2":    detail.get("ecomTag2"),
        "notes":       detail.get("notes"),
        "internet":    row.get("internet_aktif"),
        "bloke":       row.get("bloke"),
        "grade":       row.get("quality_grade"),
    }
    return hashlib.md5(
        json.dumps(key, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _build_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    """Filtre için metadata JSONB."""
    detail: Dict = row.get("detail_json") or {}
    attrs:  Dict = detail.get("attrs") or {}
    images   = detail.get("productImages") or []
    barcodes = detail.get("barcodes") or []

    # Eski nested yapıdan değer çekme yardımcısı
    def _deger(section: str, key: str):
        val = ((detail.get(section) or {}).get(key) or {}).get("deger")
        return val if val and val != "None" else None

    fit  = (attrs.get("fitName")
            or _deger("kumas", "Kalıp Tipi")
            or detail.get("fitName"))
    kumaş = (attrs.get("fabricMaterialName")
             or _deger("kumas", "Kumaş Adı")
             or row.get("fabricmaterialname"))
    tip   = (attrs.get("productTypeName")
             or _deger("temel", "Kumaş Tipi"))

    return {
        "sezon":              row.get("sezon_kodu"),
        "marka":              row.get("marka_adi"),
        "tema":               row.get("tema_adi"),
        "ana_grup":           row.get("ana_grup_adi"),
        "kategori":           row.get("urun_grubu_adi"),
        "fit":                fit,
        "kumas":              kumaş,
        "product_type":       tip,
        "style":              attrs.get("styleName"),
        "kol_uzunlugu":       attrs.get("armLengthName"),
        "yaka_tipi":          attrs.get("collarTypeName"),
        "desen":              attrs.get("fabricPatternName"),
        "uzunluk":            attrs.get("productLengthName"),
        "renk_adlari":        (attrs.get("renk_adlari") or [])[:8],
        "gorsel_sayisi":      len(images),
        "video_var":          any(img.get("type") == "video" for img in images),
        "beden_sayisi":       len([b for b in barcodes if (b.get("stock") or 0) > 0]),
        "renk_sayisi":        len(set(b.get("colorCode", "") for b in barcodes if b.get("colorCode"))),
    }


# ── DB helpers ────────────────────────────────────────────────────────────────

def _has_vector_type(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name='product_search_index' AND column_name='embedding'
        """)
        row = cur.fetchone()
        return bool(row and row[0] == 'USER-DEFINED')


def _fetch_existing_hashes(conn) -> Dict[str, str]:
    """Mevcut tüm pimland_hash'leri çek — delta için."""
    with conn.cursor() as cur:
        cur.execute("SELECT urun_kodu, pimland_hash FROM product_search_index")
        return {r[0]: r[1] for r in cur.fetchall()}


def _fetch_products(conn) -> List[Dict[str, Any]]:
    """pim_products + enrichment_quality (detail_json) + 30g satış verileri."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH satis_30g AS (
                SELECT
                    s.urun_kodu,
                    ROUND(SUM(s.tutar)::numeric, 2)                                          AS brut_ciro_30g,
                    ROUND((SUM(s.tutar) - COALESCE(ABS(SUM(d.tutar)), 0))::numeric, 2)       AS net_ciro_30g,
                    CASE WHEN SUM(s.tutar) > 0
                         THEN ROUND(COALESCE(ABS(SUM(d.tutar)), 0)::numeric / SUM(s.tutar)::numeric * 100, 1)
                         ELSE 0 END                                                          AS iade_orani
                FROM incorta_satis s
                LEFT JOIN incorta_depo_iade d ON d.urun_kodu = s.urun_kodu
                    AND d.yil = s.yil AND d.ay = s.ay
                WHERE (s.yil * 100 + s.ay) >= (
                    SELECT MAX(yil * 100 + ay) - 1
                    FROM incorta_satis
                )
                GROUP BY s.urun_kodu
            )
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
                eq.detail_json,
                COALESCE(s.brut_ciro, 0)     AS brut_ciro,
                COALESCE(g.brut_ciro_30g, 0) AS brut_ciro_30g,
                COALESCE(g.net_ciro_30g, 0)  AS net_ciro_30g,
                COALESCE(g.iade_orani, 0)    AS iade_orani
            FROM pim_products p
            LEFT JOIN enrichment_quality eq ON eq.urun_kodu = p.urun_kodu
            LEFT JOIN (
                SELECT urun_kodu, SUM(tutar) AS brut_ciro
                FROM incorta_satis WHERE yil IN (2025, 2026)
                GROUP BY urun_kodu
            ) s ON s.urun_kodu = p.urun_kodu
            LEFT JOIN satis_30g g ON g.urun_kodu = p.urun_kodu
            ORDER BY p.urun_kodu
        """)
        return [dict(r) for r in cur.fetchall()]


def _format_embedding(embedding: Optional[List[float]], use_vector: bool) -> Optional[str]:
    if embedding is None:
        return None
    if use_vector:
        return "[" + ",".join(str(v) for v in embedding) + "]"
    return json.dumps(embedding)


def _upsert_batch(conn, rows: List[Dict[str, Any]], use_vector: bool) -> None:
    emb_cast = "%s::vector" if use_vector else "%s"
    template = (
        f"(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,{emb_cast},NOW())"
    )

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
                float(r.get("brut_ciro") or 0),
                r["search_text"],
                r["pimland_hash"],
                json.dumps(r.get("metadata") or {}),
                float(r.get("brut_ciro_30g") or 0),
                float(r.get("net_ciro_30g") or 0),
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
                search_text, pimland_hash, metadata,
                brut_ciro_30g, net_ciro_30g,
                embedding, last_indexed_at
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
                pimland_hash       = EXCLUDED.pimland_hash,
                metadata           = EXCLUDED.metadata,
                brut_ciro_30g      = EXCLUDED.brut_ciro_30g,
                net_ciro_30g       = EXCLUDED.net_ciro_30g,
                embedding          = EXCLUDED.embedding,
                last_indexed_at    = NOW()
            """,
            values,
            template=template,
        )
        conn.commit()


def _create_ivfflat_index_if_needed(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM product_search_index WHERE embedding IS NOT NULL"
        )
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
    force_reindex: bool = False,
) -> Dict[str, Any]:
    """
    Ürün arama indexini oluştur / güncelle.

    force_reindex=False (varsayılan) → sadece değişen ürünleri işle (delta).
    force_reindex=True               → tüm ürünleri yeniden indexle.
    skip_embeddings=True             → embedding olmadan sadece FTS indexler.
    """
    t0 = time.perf_counter()
    log.info("indexer.started", force=force_reindex, skip_emb=skip_embeddings)

    conn = psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        products   = _fetch_products(conn)
        total      = len(products)
        use_vector = _has_vector_type(conn)
        existing   = {} if force_reindex else _fetch_existing_hashes(conn)

        log.info("indexer.fetched", total=total, use_vector=use_vector,
                 existing=len(existing))

        if not products:
            return {"status": "done", "indexed": 0, "skipped": 0,
                    "errors": 0, "elapsed_ms": 0}

        if not skip_embeddings and use_vector:
            bedrock = _bedrock_client()
            sem     = asyncio.Semaphore(_BATCH_CONCURRENT)

        indexed = skipped = errors = 0
        pending: List[Dict[str, Any]] = []

        for r in products:
            r["search_text"] = _build_search_text(r)
            r["pimland_hash"] = _compute_hash(r)
            r["metadata"]    = _build_metadata(r)
            r["embedding"]   = None

            if not force_reindex and existing.get(r["urun_kodu"]) == r["pimland_hash"]:
                skipped += 1
                continue
            pending.append(r)

        # Embedding + UPSERT — chunk bazlı
        for i in range(0, len(pending), _UPSERT_CHUNK):
            chunk = pending[i: i + _UPSERT_CHUNK]

            if not skip_embeddings and use_vector:
                embeddings = await asyncio.gather(*[
                    _embed_async(r["search_text"], bedrock, sem) for r in chunk
                ])
                for r, emb in zip(chunk, embeddings):
                    r["embedding"] = emb
                    if emb is None:
                        errors += 1

            _upsert_batch(conn, chunk, use_vector)
            indexed += len(chunk)

            if progress_cb:
                progress_cb(indexed + skipped, total)

            log.info("indexer.chunk_done",
                     indexed=indexed, skipped=skipped, total=total)

        if not skip_embeddings and use_vector:
            _create_ivfflat_index_if_needed(conn)

        elapsed = round((time.perf_counter() - t0) * 1000)
        log.info("indexer.completed",
                 indexed=indexed, skipped=skipped, errors=errors, elapsed_ms=elapsed)

        return {
            "status":     "done",
            "indexed":    indexed,
            "skipped":    skipped,
            "errors":     errors,
            "elapsed_ms": elapsed,
        }

    except Exception as exc:
        log.error("indexer.failed", error=str(exc))
        raise
    finally:
        conn.close()
