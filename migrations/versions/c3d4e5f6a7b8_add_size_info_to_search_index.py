"""add_size_info_to_search_index

Revision ID: c3d4e5f6a7b8
Revises: f7a8b9c0d1e2
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision     = 'c3d4e5f6a7b8'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on    = None


def _sp(conn, name):
    conn.execute(sa.text(f"SAVEPOINT {name}"))

def _release(conn, name):
    conn.execute(sa.text(f"RELEASE SAVEPOINT {name}"))

def _rollback(conn, name):
    conn.execute(sa.text(f"ROLLBACK TO SAVEPOINT {name}"))


def upgrade():
    conn = op.get_bind()

    # 1. size_info kolonu ekle
    _sp(conn, "size_info_col")
    try:
        conn.execute(sa.text(
            "ALTER TABLE product_search_index ADD COLUMN IF NOT EXISTS size_info TEXT"
        ))
        _release(conn, "size_info_col")
    except Exception:
        _rollback(conn, "size_info_col")

    # 2. fts_vector GENERATED ALWAYS — search_text + size_info
    # Hangi turkish config mevcut?
    try:
        row = conn.execute(sa.text(
            "SELECT cfgname FROM pg_ts_config WHERE cfgname='turkish' LIMIT 1"
        )).fetchone()
        fts_config = 'turkish' if row else 'simple'
    except Exception:
        fts_config = 'simple'

    # Mevcut GENERATED kolonu drop + yenisini size_info dahil ekle
    _sp(conn, "fts_regen")
    try:
        conn.execute(sa.text(
            "ALTER TABLE product_search_index DROP COLUMN IF EXISTS fts_vector"
        ))
        conn.execute(sa.text(f"""
            ALTER TABLE product_search_index
            ADD COLUMN fts_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector('{fts_config}',
                    coalesce(search_text, '') || ' ' || coalesce(size_info, ''))
            ) STORED
        """))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS idx_psi_fts "
            "ON product_search_index USING GIN(fts_vector)"
        ))
        _release(conn, "fts_regen")
    except Exception as exc:
        _rollback(conn, "fts_regen")
        # Eski ifade ile yeniden oluştur (size_info olmadan — degraded mode)
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
        except Exception:
            pass


def downgrade():
    conn = op.get_bind()
    try:
        row = conn.execute(sa.text(
            "SELECT cfgname FROM pg_ts_config WHERE cfgname='turkish' LIMIT 1"
        )).fetchone()
        fts_config = 'turkish' if row else 'simple'
    except Exception:
        fts_config = 'simple'

    _sp(conn, "fts_down")
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
        _release(conn, "fts_down")
    except Exception:
        _rollback(conn, "fts_down")

    try:
        conn.execute(sa.text(
            "ALTER TABLE product_search_index DROP COLUMN IF EXISTS size_info"
        ))
    except Exception:
        pass
