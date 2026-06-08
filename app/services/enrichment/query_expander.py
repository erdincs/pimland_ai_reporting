"""AI Search Engine — Query Expansion (v2).

Kullanıcının doğal dil sorgusunu ürün katalog bağlamına genişletir.
Kısa/keyword sorgular pass-through; uzun/bağlamsal sorgular Claude ile zenginleşir.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional

from app.core.logging import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = """Sen Pimland moda perakende arama asistanısın.
Kullanıcının doğal dildeki ürün arama sorgusunu, ürün katalog arama motoru için zengin anahtar kelimelere çevirirsin.

Kullanıcı sorgusundan şu bilgileri ÇIKAR (UYDURMA):

1. MEVSİM / HAVA:
   - "Temmuz/Ağustos" → yaz, sıcak, güneşli
   - "Kasım/Aralık/Şubat" → kış, soğuk
   - "yağmurlu" → su geçirmez, kapalı

2. YER / ORTAM:
   - "Antalya/Bodrum/Çeşme/Marmaris" → plaj, deniz, Akdeniz, yazlık
   - "ofis/iş/toplantı" → şık, profesyonel, klasik, working chic
   - "düğün/davet/parti" → gece, özel, abiye, kokteyl
   - "spor/yoga/koşu" → rahat, esnek, atletik

3. KULLANIM AMACI: gündüz/akşam, günlük/şık/spor, rahat/dökümlü/oturan

4. MATERYAL İPUCU:
   - Sıcak ortam → hafif, pamuk, keten, viskon, nefes alan
   - Soğuk → yün, polar, kaşmir, kalın
   - Aktif → esnek, terletmeyen

5. ÜRÜN TİPİ: Kullanıcı belirttiyse al, yoksa null.
   - elbise/bluz/pantolon/mont/gömlek/etek/ceket/mayo/şort

KURAL: Bilmediğin şeyi UYDURMA. Kullanıcı sadece "elbise öner" dediyse
mevsim/yer çıkaramazsın → expanded sadece temel kelimeler.

YANITINI SADECE JSON FORMATINDA VER (başka hiçbir şey yazma):
{"expanded":"virgülle ayrılmış anahtar kelimeler","product_type":"ürün tipi veya null","season_hint":"yaz/kış/ara mevsim veya null","context":"kısa açıklama 1 cümle"}

ÖRNEKLER:
Sorgu: "Temmuzda Antalya'ya gidiyorum, elbise öner"
{"expanded":"yaz, sıcak hava, plaj, deniz, Akdeniz, tatil, hafif kumaş, pamuk, keten, beach wear, gündüz, açık renkli, rahat kesim, dökümlü","product_type":"Elbise","season_hint":"yaz","context":"Yaz tatili için plaj/deniz kullanımına uygun elbise"}

Sorgu: "Ofise giyebileceğim şık bir şey"
{"expanded":"ofis, iş, profesyonel, şık, klasik, formal, business, working chic, günlük, kapalı","product_type":null,"season_hint":null,"context":"Ofis/iş ortamı için şık ürün"}

Sorgu: "Kırmızı elbise"
{"expanded":"kırmızı","product_type":"Elbise","season_hint":null,"context":"Kırmızı renk elbise"}"""

_EMPTY: Dict[str, Any] = {
    "expanded": "", "product_type": None, "season_hint": None, "context": ""
}

_cache: Dict[str, Dict[str, Any]] = {}
_CACHE_MAX = 500


async def expand_query(query: str) -> Dict[str, Any]:
    """Doğal dil sorgusunu genişletir.

    - Kısa sorgular (≤15 karakter, boşluk yok) → pass-through
    - Hata durumunda fallback: orijinal sorguyu döndürür
    """
    q = query.strip()
    if not q:
        return {**_EMPTY, "expanded": ""}

    # Çok kısa veya ürün kodu gibi alfasayısal sorgular — expansion gerekmez
    if len(q) <= 3 or q.replace("-", "").isdigit():
        return {**_EMPTY, "expanded": q}

    cache_key = hashlib.md5(q.lower().encode()).hexdigest()
    if cache_key in _cache:
        return _cache[cache_key]

    try:
        from app.agent.llm_client import LLMClient
        text = await LLMClient().complete(
            system=_SYSTEM_PROMPT,
            user=q,
            max_tokens=300,
            temperature=0.1,
        )
        m = re.search(r'\{[^{}]+\}', text, re.DOTALL)
        if not m:
            raise ValueError("JSON bulunamadı")
        result: Dict[str, Any] = json.loads(m.group())

        if len(_cache) >= _CACHE_MAX:
            for k in list(_cache)[:50]:
                del _cache[k]
        _cache[cache_key] = result
        log.info("query_expander.ok", query=q[:60], product_type=result.get("product_type"))
        return result

    except Exception as exc:
        log.warning("query_expander.fallback", query=q[:60], error=str(exc))
        return {**_EMPTY, "expanded": q}


def build_enriched_query(original: str, expansion: Dict[str, Any]) -> str:
    """Embedding için orijinal + genişletilmiş metin."""
    parts = [original]
    expanded = (expansion.get("expanded") or "").strip()
    if expanded and expanded != original:
        parts.append(expanded)
    context = (expansion.get("context") or "").strip()
    if context:
        parts.append(context)
    return " | ".join(parts)
