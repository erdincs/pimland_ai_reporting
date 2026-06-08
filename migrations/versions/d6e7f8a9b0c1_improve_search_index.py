"""improve_search_index — Türkçe FTS, delta indexing, metadata, RRF hazırlığı

Revision ID: d6e7f8a9b0c1
Revises: b9e3f7a2c1d8
Create Date: 2026-06-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd6e7f8a9b0c1'
down_revision: str | None = 'b9e3f7a2c1d8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sp(conn, name: str):
    conn.execute(sa.text(f"SAVEPOINT {name}"))


def _release(conn, name: str):
    conn.execute(sa.text(f"RELEASE SAVEPOINT {name}"))


def _rollback(conn, name: str):
    conn.execute(sa.text(f"ROLLBACK TO SAVEPOINT {name}"))


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. unaccent extension ─────────────────────────────────────────────────
    _sp(conn, "unaccent_sp")
    try:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS unaccent"))
        _release(conn, "unaccent_sp")
        has_unaccent = True
    except Exception:
        _rollback(conn, "unaccent_sp")
        has_unaccent = False

    # ── 2. Turkish FTS konfigürasyonu (unaccent + simple) ────────────────────
    has_turkish = False
    if has_unaccent:
        # Var mı kontrol et
        exists = conn.execute(sa.text(
            "SELECT 1 FROM pg_ts_config WHERE cfgname = 'turkish'"
        )).scalar()

        if not exists:
            _sp(conn, "turkish_sp")
            try:
                conn.execute(sa.text(
                    "CREATE TEXT SEARCH CONFIGURATION turkish (COPY = simple)"
                ))
                conn.execute(sa.text("""
                    ALTER TEXT SEARCH CONFIGURATION turkish
                    ALTER MAPPING FOR word, asciiword, asciihword,
                                     hword, hword_part, hword_asciipart
                    WITH unaccent, simple
                """))
                _release(conn, "turkish_sp")
                has_turkish = True
            except Exception:
                _rollback(conn, "turkish_sp")
        else:
            has_turkish = True

    fts_config = 'turkish' if has_turkish else 'simple'

    # ── 3. Yeni kolonlar ekle ─────────────────────────────────────────────────
    for ddl in [
        "ALTER TABLE product_search_index ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'",
        "ALTER TABLE product_search_index ADD COLUMN IF NOT EXISTS pimland_hash TEXT",
        "ALTER TABLE product_search_index ADD COLUMN IF NOT EXISTS brut_ciro_30g NUMERIC DEFAULT 0",
        "ALTER TABLE product_search_index ADD COLUMN IF NOT EXISTS net_ciro_30g NUMERIC DEFAULT 0",
        "ALTER TABLE product_search_index ADD COLUMN IF NOT EXISTS iade_orani NUMERIC DEFAULT 0",
    ]:
        conn.execute(sa.text(ddl))

    # ── 4. fts_vector → GENERATED ALWAYS AS ──────────────────────────────────
    # Eski manuel sütunu at, yenisini GENERATED olarak ekle.
    # Hata durumunda SAVEPOINT ile geri al — yeni sütun olmadan devam edilir.
    _sp(conn, "fts_regen_sp")
    try:
        conn.execute(sa.text(
            "ALTER TABLE product_search_index DROP COLUMN IF EXISTS fts_vector"
        ))
        conn.execute(sa.text(f"""
            ALTER TABLE product_search_index
            ADD COLUMN fts_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector('{fts_config}', coalesce(search_text, ''))
            ) STORED
        """))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS idx_psi_fts "
            "ON product_search_index USING GIN(fts_vector)"
        ))
        _release(conn, "fts_regen_sp")
    except Exception:
        _rollback(conn, "fts_regen_sp")

    # ── 5. metadata GIN index ─────────────────────────────────────────────────
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_psi_metadata "
        "ON product_search_index USING GIN(metadata)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_psi_hash "
        "ON product_search_index(pimland_hash)"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    for col in ["metadata", "pimland_hash", "brut_ciro_30g", "net_ciro_30g", "iade_orani"]:
        conn.execute(sa.text(
            f"ALTER TABLE product_search_index DROP COLUMN IF EXISTS {col}"
        ))
