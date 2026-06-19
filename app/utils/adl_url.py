"""adL ürün URL üretici ve CDN thumbnail yardımcıları — adL Premium Brief.

Kategori slug Pimland API'den gelir (bkz. pimland_category.py).
Bu modül slug'ı parametre olarak alır, kendi başına lookup yapmaz.
"""
from __future__ import annotations

import re

CDN_BASE  = "https://img-adl.sm.mncdn.com/cdnimages/products"
SITE_BASE = "https://www.adl.com.tr/tr"

_TR_MAP = str.maketrans(
    "ıİğĞüÜşŞöÖçÇ",
    "iIgGuUsSoOcC",
)


def slugify(text: str) -> str:
    """Türkçe metin → URL-safe slug."""
    t = text.lower().translate(_TR_MAP)
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


def urun_url(kategori_slug: str, urun_adi: str, stock_code: str, color_code: str) -> str:
    """https://www.adl.com.tr/tr/{kat-slug}/{urun-slug}-p-{stockCode}-{colorCode}

    kategori_slug: Pimland category_slug_for(urun_kodu) çıktısı (örn. 'elbise').
    """
    slug = slugify(urun_adi)
    kat  = kategori_slug or "urun"
    return f"{SITE_BASE}/{kat}/{slug}-p-{stock_code}-{color_code}"


def urun_thumb_url(stock_code: str, color_code: str) -> str:
    """CDN thumbnail URL. CDN 404 → onerror JS mock gradient'e düşer."""
    return f"{CDN_BASE}/{stock_code}_{color_code}_1.jpg"
