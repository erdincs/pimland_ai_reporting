"""Dosya içeriğini Bedrock Converse API mesajına enjekte eder.

Bedrock Converse API content formatı:
  {"type": "text", "text": "..."}
  {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
  {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": ...}, "title": ...}
"""

from __future__ import annotations

from typing import Any, Dict, List, Union

from app.core.logging import get_logger

log = get_logger(__name__)

_EXCEL_SYSTEM_ADDENDUM = """
Kullanıcı veri dosyası yükledi. Bu veriler geçici PostgreSQL tablolarında mevcut.
Tablo adlarını sorgu için kullanabilirsin — mevcut text-to-SQL altyapısı çalıştıracak.
Veriyi analiz ederken önce kolonları ve örnek satırları göster, sonra yanıt ver.
"""


def build_message_with_files(
    user_text: str,
    session_files: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Bedrock Converse API için content listesi oluştur.
    Dosyalar önce, kullanıcı sorusu en sona gelir.
    """
    content: List[Dict[str, Any]] = []
    has_dataframe = False

    for f in session_files:
        ftype = f.get("type")
        fname = f.get("filename", "dosya")

        if ftype == "document":
            text = f.get("text", "")
            if not text:
                continue
            uyari = "\n\n[NOT: Dosya çok uzundu, ilk 50.000 karakter alındı.]" if f.get("truncated") else ""
            content.append({
                "type":   "document",
                "source": {
                    "type":       "text",
                    "media_type": "text/plain",
                    "data":       text + uyari,
                },
                "title": fname,
            })

        elif ftype == "image":
            content.append({
                "type":   "image",
                "source": {
                    "type":       "base64",
                    "media_type": f.get("mime", "image/jpeg"),
                    "data":       f.get("base64", ""),
                },
            })

        elif ftype == "dataframe":
            has_dataframe = True
            parcalar = []
            for t in f.get("tables", []):
                cols    = ", ".join(t.get("columns", []))
                parcalar.append(
                    f"📊 **{t['sheet']}** — {t['rows']:,} satır\n"
                    f"PostgreSQL tablosu: `{t['pg_table']}`\n"
                    f"Kolonlar: {cols}\n\n"
                    f"İlk 5 satır:\n{t.get('preview', '')}\n\n"
                    f"İstatistik:\n{t.get('stats', '')}"
                )
            content.append({
                "type": "text",
                "text": f"**Yüklenen veri dosyası: {fname}**\n\n" + "\n---\n".join(parcalar),
            })

    # Kullanıcı sorusunu ekle
    content.append({"type": "text", "text": user_text})

    log.info(
        "file_aware_agent.built",
        files=len(session_files),
        has_dataframe=has_dataframe,
        content_blocks=len(content),
    )
    return content, has_dataframe


def get_system_addendum(has_dataframe: bool) -> str:
    """Dosya varsa system prompt'a eklenecek ek talimat."""
    return _EXCEL_SYSTEM_ADDENDUM if has_dataframe else ""
