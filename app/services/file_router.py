"""Dosya + soru kombinasyonuna göre en uygun yaklaşımı seçer.

Yaklaşımlar:
  A — batch_mcp   : Ürün bazlı sorular → MCP'den paralel çek
  B — sql         : Toplu/istatistik sorular → temp tabloya SQL
  C — context     : Dosya bağlam, ek sorgu gerekmez
  D — hybrid      : Birden fazla yaklaşım gerekiyor
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ── Karar kelime listeleri ────────────────────────────────────────────────────

_BATCH_MCP_KEYWORDS = {
    # Stok / beden / ürün detayı → MCP gerekli
    "stok", "beden", "ölçü", "olcu", "kargo", "kumaş", "kumas",
    "materyal", "renk", "fiyat", "price", "ne zaman", "var mı",
    "mevcut", "gelir mi", "olur mu", "uyar mı", "uygundur",
    "önerirsiniz", "almak", "sipariş", "teslim",
    "hangi beden", "kaç beden", "ürün bilgi", "urun bilgi",
    "detay", "içerik", "bilgi ver", "anlat",
}

_SQL_KEYWORDS = {
    # Toplu analiz → SQL
    "toplam", "ortalama", "en çok", "en az", "kaç tane",
    "listele", "dağılım", "yüzde", "%", "analiz", "özet",
    "hangi kategori", "hangi marka", "hangi sezon",
    "istatistik", "rapor", "karşılaştır", "sırala",
}

_CONTEXT_KEYWORDS = {
    # Bağlam yeterli
    "ortak özellik", "genel", "özellikleri", "benzerlikleri",
    "farkları", "kategorile", "grupla", "sınıflandır",
}


def _has_code_column(tables: List[Dict]) -> Optional[str]:
    """Tablolarda ürün kodu sütunu var mı? Varsa kolon adını döndür."""
    code_patterns = re.compile(
        r"(model[\s_]?kod|stock[\s_]?code|sku|ur[uü]n[\s_]?kod|item[\s_]?code"
        r"|product[\s_]?code|barkod|barcode|stok[\s_]?kod)",
        re.I | re.UNICODE
    )
    for t in tables:
        for col in t.get("columns", []):
            col_str = str(col).lower()
            if code_patterns.search(col_str):
                return col
            # Doğrudan eşleşmeler
            if col_str in ("model kodu", "model kodu", "ürün kodu", "urun kodu",
                           "stok kodu", "sku", "kod", "code", "itemcode", "item code"):
                return col
    return None


def _has_numeric_columns(tables: List[Dict]) -> bool:
    """Sayısal analiz yapılabilecek sütun var mı?"""
    numeric_hints = re.compile(
        r"(tutar|fiyat|adet|miktar|ciro|revenue|amount|quantity|count|sum)",
        re.I
    )
    for t in tables:
        for col in t.get("columns", []):
            if numeric_hints.search(str(col)):
                return True
    return False


def decide_approach(
    question: str,
    session_files: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    """
    Soru + dosyalar → yaklaşım kodu ve meta bilgisi.

    Döner: (approach, meta)
      approach: 'batch_mcp' | 'sql' | 'context' | 'hybrid'
      meta: {code_column, tables, pg_tables, row_count, ...}
    """
    # Dataframe dosyaları filtrele
    df_files = [f for f in session_files if f.get("type") == "dataframe"]
    if not df_files:
        return "context", {}

    all_tables = [t for f in df_files for t in f.get("tables", [])]
    pg_tables  = [t["pg_table"] for t in all_tables]
    total_rows = sum(t.get("rows", 0) for t in all_tables)
    code_col   = _has_code_column(all_tables)
    has_nums   = _has_numeric_columns(all_tables)

    q_lower = question.lower()

    # Kelime skorlama
    batch_score   = sum(1 for kw in _BATCH_MCP_KEYWORDS if kw in q_lower)
    sql_score     = sum(1 for kw in _SQL_KEYWORDS       if kw in q_lower)
    context_score = sum(1 for kw in _CONTEXT_KEYWORDS   if kw in q_lower)

    meta = {
        "code_column": code_col,
        "pg_tables":   pg_tables,
        "all_tables":  all_tables,
        "total_rows":  total_rows,
        "has_nums":    has_nums,
    }

    # D: Hibrit — hem MCP hem SQL gerekli
    if batch_score >= 2 and sql_score >= 2 and code_col:
        return "hybrid", meta

    # A: Batch MCP — ürün kodu var ve sorgu ürün odaklı (max 500 satır)
    if code_col and batch_score > sql_score and total_rows <= 500:
        return "batch_mcp", meta

    # B: SQL — sayısal/toplu sorular
    if sql_score > batch_score and (has_nums or sql_score >= 2):
        return "sql", meta

    # C: Bağlam — diğer
    if context_score > 0 or (not code_col and not has_nums):
        return "context", meta

    # Default: kod varsa MCP, yoksa SQL
    if code_col and total_rows <= 200:
        return "batch_mcp", meta
    return "sql", meta
