"""add_menu_tables — menu_agents, menu_groups, menu_items + seed data

Revision ID: e8f9a0b1c2d3
Revises: f7a8b9c0d1e2
Create Date: 2026-06-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menu_agents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("subtitle", sa.String(200), server_default=""),
        sa.Column("icon", sa.String(16), server_default="🤖"),
        sa.Column("badge", sa.String(16), server_default=""),
        sa.Column("badge_color", sa.String(32), server_default="#ffffff"),
        sa.Column("active", sa.Boolean, server_default="true"),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("now()")),
    )

    op.create_table(
        "menu_groups",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("icon", sa.String(16), server_default="📁"),
        sa.Column("active", sa.Boolean, server_default="true"),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("now()")),
    )

    op.create_table(
        "menu_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("group_id", sa.String(64), sa.ForeignKey("menu_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("icon", sa.String(16), server_default="📄"),
        sa.Column("active", sa.Boolean, server_default="true"),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("now()")),
    )
    op.create_index("ix_menu_items_group_id", "menu_items", ["group_id"])

    # ── Seed data ───────────────────────────────────────────────────────────
    agents_tbl = sa.table("menu_agents",
        sa.column("id"), sa.column("label"), sa.column("subtitle"),
        sa.column("icon"), sa.column("badge"), sa.column("badge_color"),
        sa.column("active"), sa.column("sort_order"),
    )
    op.bulk_insert(agents_tbl, [
        {"id": "agent-1", "label": "Pimland AI Agent",  "subtitle": "Veri analiz asistanı",   "icon": "🤖", "badge": "AI", "badge_color": "#4FC3F7", "active": True, "sort_order": 0},
        {"id": "agent-2", "label": "Call Center Agent", "subtitle": "Ürün bilgi asistanı",     "icon": "📞", "badge": "CC", "badge_color": "#81C784", "active": True, "sort_order": 1},
        {"id": "agent-3", "label": "Sizewin Agent",     "subtitle": "Beden öneri sistemi",     "icon": "👗", "badge": "SW", "badge_color": "#CE93D8", "active": True, "sort_order": 2},
    ])

    groups_tbl = sa.table("menu_groups",
        sa.column("id"), sa.column("label"), sa.column("icon"),
        sa.column("active"), sa.column("sort_order"),
    )
    op.bulk_insert(groups_tbl, [
        {"id": "grp-1", "label": "SATIŞ ANALİZ",       "icon": "📊", "active": True, "sort_order": 0},
        {"id": "grp-2", "label": "E-TİCARET",           "icon": "🛒", "active": True, "sort_order": 1},
        {"id": "grp-3", "label": "ÜRÜN YÖNETİMİ",       "icon": "🏷️", "active": True, "sort_order": 2},
        {"id": "grp-4", "label": "KATEGORİ PLANLAMA",   "icon": "🗂️", "active": True, "sort_order": 3},
        {"id": "grp-5", "label": "ÜRÜN ZENGİNLEŞTİRME", "icon": "✨", "active": True, "sort_order": 4},
        {"id": "grp-6", "label": "ADL RAPORLAR",         "icon": "📑", "active": True, "sort_order": 5},
        {"id": "grp-7", "label": "YÖNETİM",              "icon": "⚙️", "active": True, "sort_order": 6},
        {"id": "grp-8", "label": "DİĞER",                "icon": "⚙️", "active": True, "sort_order": 7},
    ])

    items_tbl = sa.table("menu_items",
        sa.column("id"), sa.column("group_id"), sa.column("label"),
        sa.column("icon"), sa.column("active"), sa.column("sort_order"),
    )
    op.bulk_insert(items_tbl, [
        # grp-1: SATIŞ ANALİZ
        {"id": "itm-1",  "group_id": "grp-1", "label": "Günlük Analiz",          "icon": "📅", "active": True, "sort_order": 0},
        {"id": "itm-2",  "group_id": "grp-1", "label": "Yönetici Özeti",          "icon": "👥", "active": True, "sort_order": 1},
        {"id": "itm-3",  "group_id": "grp-1", "label": "Mağaza Performans",       "icon": "🏪", "active": True, "sort_order": 2},
        {"id": "itm-4",  "group_id": "grp-1", "label": "Dönemsel Performans",     "icon": "📈", "active": True, "sort_order": 3},
        {"id": "itm-5",  "group_id": "grp-1", "label": "Dönemsel Karşılaştırma", "icon": "🔄", "active": True, "sort_order": 4},
        # grp-2: E-TİCARET
        {"id": "itm-6",  "group_id": "grp-2", "label": "Sipariş Takip",           "icon": "📦", "active": True, "sort_order": 0},
        {"id": "itm-7",  "group_id": "grp-2", "label": "Trafik Analiz",           "icon": "🌐", "active": True, "sort_order": 1},
        {"id": "itm-8",  "group_id": "grp-2", "label": "Dönüşüm Oranları",       "icon": "🎯", "active": True, "sort_order": 2},
        # grp-3: ÜRÜN YÖNETİMİ
        {"id": "itm-9",  "group_id": "grp-3", "label": "Ürün Listesi",            "icon": "📋", "active": True, "sort_order": 0},
        {"id": "itm-10", "group_id": "grp-3", "label": "Stok Durumu",             "icon": "📊", "active": True, "sort_order": 1},
        # grp-4: KATEGORİ PLANLAMA
        {"id": "itm-11", "group_id": "grp-4", "label": "Kategori Ağacı",          "icon": "🌳", "active": True, "sort_order": 0},
        {"id": "itm-12", "group_id": "grp-4", "label": "Eşleştirme",              "icon": "🔗", "active": True, "sort_order": 1},
        # grp-5: ÜRÜN ZENGİNLEŞTİRME
        {"id": "itm-13", "group_id": "grp-5", "label": "Görsel Analiz",           "icon": "🖼️", "active": True, "sort_order": 0},
        {"id": "itm-14", "group_id": "grp-5", "label": "Attribute Tarama",        "icon": "🔍", "active": True, "sort_order": 1},
        # grp-6: ADL RAPORLAR
        {"id": "itm-15", "group_id": "grp-6", "label": "Günlük Brief",            "icon": "📰", "active": True, "sort_order": 0},
        {"id": "itm-16", "group_id": "grp-6", "label": "Haftalık Özet",           "icon": "📝", "active": True, "sort_order": 1},
        # grp-7: YÖNETİM
        {"id": "itm-17", "group_id": "grp-7", "label": "Brief Profilleri",        "icon": "⚙️", "active": True, "sort_order": 0},
        {"id": "itm-18", "group_id": "grp-7", "label": "Soru Havuzu",             "icon": "📚", "active": True, "sort_order": 1},
        # grp-8: DİĞER
        {"id": "itm-19", "group_id": "grp-8", "label": "Ayarlar",                 "icon": "🔧", "active": True, "sort_order": 0},
    ])


def downgrade() -> None:
    op.drop_table("menu_items")
    op.drop_table("menu_groups")
    op.drop_table("menu_agents")
