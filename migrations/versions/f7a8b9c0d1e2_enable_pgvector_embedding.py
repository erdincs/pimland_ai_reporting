"""enable_pgvector_embedding — TEXT → vector(1024), ivfflat index

Revision ID: f7a8b9c0d1e2
Revises: d6e7f8a9b0c1
Create Date: 2026-06-08

pgvector/pgvector:pg14 image'ına geçildikten sonra çalıştırılır.
embedding kolonu TEXT'ten vector(1024)'e dönüştürülür (tüm değerler NULL).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f7a8b9c0d1e2'
down_revision: str | None = 'd6e7f8a9b0c1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sp(conn, name): conn.execute(sa.text(f"SAVEPOINT {name}"))
def _rel(conn, name): conn.execute(sa.text(f"RELEASE SAVEPOINT {name}"))
def _rb(conn, name):  conn.execute(sa.text(f"ROLLBACK TO SAVEPOINT {name}"))


def upgrade() -> None:
    conn = op.get_bind()

    # 1. pgvector extension
    _sp(conn, "vec_ext")
    try:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        _rel(conn, "vec_ext")
        has_vector = True
    except Exception:
        _rb(conn, "vec_ext")
        has_vector = False

    if not has_vector:
        # pgvector yüklü değil — migration sessizce geçer, kolon TEXT kalır
        return

    # 2. Mevcut kolon tipi kontrol
    col_type = conn.execute(sa.text("""
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'product_search_index' AND column_name = 'embedding'
    """)).scalar()

    if col_type == 'USER-DEFINED':
        # Zaten vector — bir şey yapma
        return

    # 3. TEXT → vector(1024): drop + add (tüm değerler NULL olduğu için güvenli)
    _sp(conn, "col_convert")
    try:
        conn.execute(sa.text(
            "ALTER TABLE product_search_index DROP COLUMN IF EXISTS embedding"
        ))
        conn.execute(sa.text(
            "ALTER TABLE product_search_index ADD COLUMN embedding vector(1024)"
        ))
        _rel(conn, "col_convert")
    except Exception as e:
        _rb(conn, "col_convert")
        raise RuntimeError(f"embedding kolon dönüşümü başarısız: {e}") from e

    # 4. Eski FTS index'ini koru, ivfflat index için satır sayısını bekle
    # (index run_indexer tamamlandıktan sonra product_indexer tarafından oluşturulur)


def downgrade() -> None:
    conn = op.get_bind()
    _sp(conn, "down_sp")
    try:
        conn.execute(sa.text(
            "ALTER TABLE product_search_index DROP COLUMN IF EXISTS embedding"
        ))
        conn.execute(sa.text(
            "ALTER TABLE product_search_index ADD COLUMN embedding TEXT"
        ))
        _rel(conn, "down_sp")
    except Exception:
        _rb(conn, "down_sp")
