"""add_product_search_index

Revision ID: b9e3f7a2c1d8
Revises: 38494b84ec8f
Create Date: 2026-06-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b9e3f7a2c1d8'
down_revision: str | None = '38494b84ec8f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector extension — Docker (production) image'ında mevcut.
    # Lokal geliştirmede yoksa SAVEPOINT ile transaction kurtarılır,
    # embedding kolonu TEXT olarak oluşturulur; FTS hâlâ çalışır.
    conn = op.get_bind()
    has_vector = False
    conn.execute(sa.text("SAVEPOINT before_vector"))
    try:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(sa.text("RELEASE SAVEPOINT before_vector"))
        has_vector = True
    except Exception:
        conn.execute(sa.text("ROLLBACK TO SAVEPOINT before_vector"))

    embedding_col = "embedding vector(1024)" if has_vector else "embedding TEXT"

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS product_search_index (
            urun_kodu           TEXT PRIMARY KEY,
            urun_adi            TEXT,
            marka_adi           TEXT,
            sezon_kodu          TEXT,
            sezon_adi           TEXT,
            tema_adi            TEXT,
            ana_grup_adi        TEXT,
            urun_grubu_adi      TEXT,
            fabricmaterialname  TEXT,
            color_codes         TEXT,
            default_image_url   TEXT,
            quality_grade       TEXT,
            quality_score       INTEGER,
            internet_aktif      BOOLEAN DEFAULT true,
            bloke               BOOLEAN DEFAULT false,
            brut_ciro           NUMERIC DEFAULT 0,
            search_text         TEXT,
            fts_vector          tsvector,
            {embedding_col},
            last_indexed_at     TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_psi_fts ON product_search_index USING GIN(fts_vector)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_psi_marka ON product_search_index(marka_adi)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_psi_sezon ON product_search_index(sezon_kodu)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_psi_grade ON product_search_index(quality_grade)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_psi_internet ON product_search_index(internet_aktif)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS product_search_index")
