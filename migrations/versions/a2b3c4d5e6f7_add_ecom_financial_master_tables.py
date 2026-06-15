"""add_ecom_financial_master_tables

Revision ID: a2b3c4d5e6f7
Revises: c3d4e5f6a7b8
Create Date: 2026-06-10
"""
from __future__ import annotations
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'a2b3c4d5e6f7'
down_revision: str | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Master data ───────────────────────────────────────────────────────────
    op.create_table('pim_main_product_groups',
        sa.Column('reference_code', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('translations', sa.Text(), nullable=True),
        sa.Column('sync_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('reference_code'),
    )

    op.create_table('pim_product_themes',
        sa.Column('reference_code', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('translations', sa.Text(), nullable=True),
        sa.Column('sync_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('reference_code'),
    )

    op.create_table('pim_product_story_master',
        sa.Column('reference_code', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('translations', sa.Text(), nullable=True),
        sa.Column('sync_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('reference_code'),
    )

    # ── E-ticaret katalog + kategori yapısı ───────────────────────────────────
    op.create_table('pim_ecom_catalogs',
        sa.Column('catalog_code', sa.Text(), nullable=False),
        sa.Column('catalog_name', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('raw_json', JSONB(), nullable=True),
        sa.Column('sync_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('catalog_code'),
    )

    op.create_table('pim_ecom_category_tree',
        sa.Column('catalog_code', sa.Text(), nullable=False),
        sa.Column('category_code', sa.Text(), nullable=False),
        sa.Column('category_id', sa.Text(), nullable=True),
        sa.Column('category_name', sa.Text(), nullable=True),
        sa.Column('parent_id', sa.Text(), nullable=True),
        sa.Column('level', sa.Integer(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('raw_json', JSONB(), nullable=True),
        sa.Column('sync_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('catalog_code', 'category_code'),
    )
    op.create_index('ix_ecom_cat_tree_catalog', 'pim_ecom_category_tree', ['catalog_code'])
    op.create_index('ix_ecom_cat_tree_parent', 'pim_ecom_category_tree', ['catalog_code', 'parent_id'])

    op.create_table('pim_ecom_category_products',
        sa.Column('catalog_code', sa.Text(), nullable=False),
        sa.Column('category_code', sa.Text(), nullable=False),
        sa.Column('urun_kodu', sa.Text(), nullable=False),
        sa.Column('renk_kodu', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('sync_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('catalog_code', 'category_code', 'urun_kodu', 'renk_kodu'),
    )
    op.create_index('ix_ecom_cat_prod_urun', 'pim_ecom_category_products', ['urun_kodu'])
    op.create_index('ix_ecom_cat_prod_cat', 'pim_ecom_category_products', ['catalog_code', 'category_code'])

    op.create_table('pim_ecom_page_planner',
        sa.Column('catalog_code', sa.Text(), nullable=False),
        sa.Column('category_code', sa.Text(), nullable=False),
        sa.Column('urun_kodu', sa.Text(), nullable=False),
        sa.Column('renk_kodu', sa.Text(), nullable=False),
        sa.Column('sira_no', sa.Integer(), nullable=True),
        sa.Column('display_type', sa.Text(), nullable=True),
        sa.Column('raw_json', JSONB(), nullable=True),
        sa.Column('sync_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('catalog_code', 'category_code', 'urun_kodu', 'renk_kodu'),
    )
    op.create_index('ix_ecom_planner_urun', 'pim_ecom_page_planner', ['urun_kodu'])

    # ── Materialized view ─────────────────────────────────────────────────────
    op.execute("""
        CREATE MATERIALIZED VIEW mv_ecom_product_placement AS
        SELECT
            cp.catalog_code,
            cat.category_name,
            cp.category_code,
            parent.category_name AS ust_kategori_adi,
            parent.category_code AS ust_kategori_kodu,
            cp.urun_kodu,
            cp.renk_kodu,
            pp.sira_no,
            pp.display_type,
            p.urun_adi,
            p.marka_kodu,
            p.marka_adi,
            p.sezon_kodu,
            p.sezon_adi,
            p.urun_grubu_kodu,
            p.urun_grubu_adi,
            p.ana_grup_kodu,
            p.ana_grup_adi,
            p.tema_kodu,
            p.internet_aktif,
            p.bloke,
            p.default_image_url
        FROM pim_ecom_category_products cp
        LEFT JOIN pim_ecom_category_tree cat
            ON cat.catalog_code = cp.catalog_code
            AND cat.category_code = cp.category_code
        LEFT JOIN pim_ecom_category_tree parent
            ON parent.catalog_code = cp.catalog_code
            AND parent.category_id = cat.parent_id
        LEFT JOIN pim_ecom_page_planner pp
            ON pp.catalog_code = cp.catalog_code
            AND pp.category_code = cp.category_code
            AND pp.urun_kodu = cp.urun_kodu
            AND pp.renk_kodu = cp.renk_kodu
        LEFT JOIN pim_products p ON p.urun_kodu = cp.urun_kodu
        WITH DATA
    """)
    op.execute("""
        CREATE UNIQUE INDEX uix_mv_ecom_placement
        ON mv_ecom_product_placement (catalog_code, category_code, urun_kodu, renk_kodu)
    """)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_ecom_product_placement")
    op.drop_table('pim_ecom_page_planner')
    op.drop_table('pim_ecom_category_products')
    op.drop_table('pim_ecom_category_tree')
    op.drop_table('pim_ecom_catalogs')
    op.drop_table('pim_product_story_master')
    op.drop_table('pim_product_themes')
    op.drop_table('pim_main_product_groups')
