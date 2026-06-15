"""add_product_malzeme

Revision ID: b1c2d3e4f5a6
Revises: a2b3c4d5e6f7
Create Date: 2026-06-10
"""
from __future__ import annotations
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = 'b1c2d3e4f5a6'
down_revision: str | None = 'a2b3c4d5e6f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('pim_product_malzeme',
        sa.Column('urun_kodu',          sa.Text(), nullable=False),
        sa.Column('malzeme_tipi',       sa.Text(), nullable=False),   # 'ana' | 'yardimci'
        sa.Column('malzeme_grup_kodu',  sa.Text(), nullable=False),
        sa.Column('malzeme_grup_adi',   sa.Text(), nullable=True),
        sa.Column('stok_kodu',          sa.Text(), nullable=True),
        sa.Column('dis_malzeme',        sa.Text(), nullable=True),
        sa.Column('miktar',             sa.Float(), nullable=True),
        sa.Column('birim_fiyat',        sa.Float(), nullable=True),
        sa.Column('birim_adi',          sa.Text(), nullable=True),
        sa.Column('birim_kodu',         sa.Text(), nullable=True),
        sa.Column('fire_orani',         sa.Float(), nullable=True),
        sa.Column('doviz',              sa.Text(), nullable=True),
        sa.Column('kur',                sa.Float(), nullable=True),
        sa.Column('toplam_tl',          sa.Float(), nullable=True),
        sa.Column('doviz_degeri',       sa.Float(), nullable=True),
        sa.Column('renk_adi',           sa.Text(), nullable=True),
        sa.Column('renk_kodu',          sa.Text(), nullable=True),
        sa.Column('sync_updated_at',    sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('urun_kodu', 'malzeme_tipi', 'malzeme_grup_kodu'),
    )
    op.create_index('ix_product_malzeme_urun', 'pim_product_malzeme', ['urun_kodu'])
    op.create_index('ix_product_malzeme_tip',  'pim_product_malzeme', ['malzeme_tipi'])

    op.execute("""
        CREATE MATERIALIZED VIEW mv_product_malzeme_ozet AS
        SELECT
            m.urun_kodu,
            p.urun_adi,
            p.marka_kodu,
            p.marka_adi,
            p.sezon_kodu,
            p.sezon_adi,
            SUM(CASE WHEN m.malzeme_tipi = 'ana'      THEN m.toplam_tl ELSE 0 END) AS ana_malzeme_toplam,
            SUM(CASE WHEN m.malzeme_tipi = 'yardimci' THEN m.toplam_tl ELSE 0 END) AS yardimci_malzeme_toplam,
            COUNT(DISTINCT CASE WHEN m.malzeme_tipi = 'ana'      THEN m.malzeme_grup_kodu END) AS ana_malzeme_sayisi,
            COUNT(DISTINCT CASE WHEN m.malzeme_tipi = 'yardimci' THEN m.malzeme_grup_kodu END) AS yardimci_malzeme_sayisi
        FROM pim_product_malzeme m
        LEFT JOIN pim_products p ON p.urun_kodu = m.urun_kodu
        GROUP BY m.urun_kodu, p.urun_adi, p.marka_kodu, p.marka_adi, p.sezon_kodu, p.sezon_adi
        WITH DATA
    """)
    op.execute("""
        CREATE UNIQUE INDEX uix_mv_product_malzeme_ozet ON mv_product_malzeme_ozet (urun_kodu)
    """)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_product_malzeme_ozet")
    op.drop_index('ix_product_malzeme_tip',  table_name='pim_product_malzeme')
    op.drop_index('ix_product_malzeme_urun', table_name='pim_product_malzeme')
    op.drop_table('pim_product_malzeme')
