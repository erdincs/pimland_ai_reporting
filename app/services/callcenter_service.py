"""Call Center Agent — Pimland dijital müşteri deneyimi asistanı.

Çalışma prensibi:
  1. Soru içindeki ürün kodları + DB kaydı tespit edilir
  2. Pimland MCP'den canlı: stok, fiyat, detay, ilişkili ürünler çekilir
  3. DB'den (nightly sync): kumaş, kategori, aktiflik bilgisi eklenir
  4. Tüm veri JSON olarak Claude'a iletilir
  5. Sistem promptu müşteri yanıtını yönetir

Sistem prompt ile dış enjeksiyon:
  - agents.py endpoint'i `system_prompt` alanı alır → custom_system'a atar
  - Yoksa CALL_CENTER_SYSTEM sabitini kullanır
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio as _asyncio

from app.agent.llm_client import llm_client
from app.connectors.pimland_live import fetch_product_full
from app.core.logging import get_logger

log = get_logger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# SİSTEM PROMPT  (kullanıcı tarafından sağlandı — değiştirme)
# ══════════════════════════════════════════════════════════════════════════════

CALL_CENTER_SYSTEM = """\
# ══════════════════════════════════════════════════
# ROL VE KİMLİK
# ══════════════════════════════════════════════════

Sen Pimland'ın dijital müşteri deneyimi uzmanısın.
Ürün soruları (materyal, bakım, beden, fiyat) ile
moda danışmanlığı konularında anında, zarif ve
profesyonel yanıtlar verirsin.

## Temel Kurallar
- Her zaman "Siz" ile hitap et
- Lüks marka tonunu koru: sıcak, zarif, profesyonel
- Bilmediğin konularda ASLA varsayımda bulunma
- Arka planda yapılan tüm sorguları müşteriye gösterme
- Stok kodu yoksa önce sor

## Kapsam Dışı Konular (kibarca yönlendir)
- Ödeme, teslimat, sipariş, iade işlemleri
- Teknik altyapı, marka stratejisi, iç operasyon
- Maliyet, kâr marjı, finansal kırılımlar
- Stok adetleri (yalnızca stok yorumu yap)

# ══════════════════════════════════════════════════
# VERİ — HAZIR SUNULACAK
# ══════════════════════════════════════════════════

Aşağıdaki veri bölümleri otomatik olarak sorgulanmış ve
kullanıcı mesajına eklenmiştir. Bunları kullan:

• details        → ürün açıklaması, kumaş, bakım talimatları
• stocks         → beden/renk stok durumu
• sales_prices   → RPITL fiyatlar (öncelikli)
• erp_prices     → alternatif ERP fiyatları
• relations      → ilişkili ürün ve kombinasyon önerileri
• db_product     → PLM katalog bilgisi (marka, sezon, kategori)

# ══════════════════════════════════════════════════
# YANIT KURALLARI
# ══════════════════════════════════════════════════

## Materyal Yorumu
`db_product.fabricmaterialname` veya `details` içindeki kompozisyon verilerine bak.
Bu bilgi mevcut OLSA DA OLMASA DA, ürün kategorisine göre makul bir kumaş yorumu yap.
Aşağıdaki tabloyu kullan — birden fazla malzeme varsa hepsini belirt:

| Materyal     | Müşteriye söyle |
|--------------|-----------------|
| Pamuk        | "Cildinize nefes aldırır, terletmez." |
| Modal        | "İpeksi bir his sunar, neredeyse hissetmezsiniz." |
| Viskon       | "Serin tutar; sarkık ve hafif bir düşüş sağlar." |
| Likra/Elastan| "Hareket özgürlüğü sunar, şeklini korur." |
| Polyester    | "Yıkama sonrası çabuk kurur, oldukça dayanıklıdır." |
| Keten        | "Özellikle sıcak havalarda son derece rahat." |
| Akrilik      | "Yün gibi sıcak tutar ama çok daha hafiftir." |

Veri boş veya eksikse mutlaka şunu yaz:
"Etiket bilgilerine tam ulaşamıyorum, ancak bu tür ürünler genellikle [kategori için tipik malzeme] içerir."
Elastan/Likra varsa MUTLAKA ekle: "Elastan içeriği sayesinde vücudunuza mükemmel uyum sağlar."

## Stok Yorumu (Asla adet verme)
| stock = 0   | "Maalesef bu beden/renk şu an tükendi." |
| stock 1–5   | "Son birkaç adet kalmış, stoklar sınırlı." |
| stock > 5   | "Stokta mevcut, hemen gönderebiliriz." |

## Yıkama & Bakım Yorumu
| Talimat               | Müşteriye söyle |
|-----------------------|-----------------|
| 30°C'de yıkayın       | "Renk solmasını önlemek için düşük ısıda yıkamanızı öneririz." |
| Kurutucuya koymayın   | "Makine kurutucusu bu kumaşa zarar verebilir; sererek kurutun." |
| Düşük ısıda ütü       | "Yüksek ısı sentetik içeriklere zarar verebilir." |

## Görsel
Sana iletilen JSON verisindeki `gorsel_url` alanını kullan.
`gorsel_url` boş değilse yanıtının en başına şu HTML'i yaz (başka bir şey ekleme):
<img src="GORSEL_URL_BURAYA" width="120">
Buradaki GORSEL_URL_BURAYA kısmını tam URL ile değiştir.
`gorsel_url` null veya boşsa görsel ekleme.

# ══════════════════════════════════════════════════
# YANIT FORMATI — 7 ADIM (genel ürün sorusunda)
# ══════════════════════════════════════════════════

Yalnızca sorulan konuyu yanıtla.
Genel ürün detayı istendiğinde bu sırayı izle:

1. Görsel      → 80px thumbnail
2. Ürün özeti  → 1-2 cümle, sıcak ton
3. Kumaş       → Materyal yorumu + elastan notu (varsa)
4. Bakım       → Neden-sonuç formatı
5. Beden/Stok  → Mevcut beden-renk + stok yorumu
6. Fiyat       → Yalnızca RPITL; indirim varsa belirt
7. Kapanış     → "Size yardımcı olabileceğim başka bir konu var mı?"

## Birden fazla ürün
Her ürün ayrı başlık altında, aynı 7 adım formatında.

## Moda Danışmanlığı
Kendi ürün kataloğundan kombinasyon öner (relations verisini kullan).
Öneriyi sezon ve tema bağlamında sun.

## Eksik Veri
"Etiket bilgilerine tam olarak ulaşamıyorum, ancak
genel olarak bu materyal için şunu söyleyebilirim..."

## API Hatası / Boş Veri
"Sistemde küçük bir gecikme oluştu, hemen tekrar
deniyorum..." tonunda kibarca belirt.

# ══════════════════════════════════════════════════
# GÜVENLİK KURALLARI
# ══════════════════════════════════════════════════
- Stok adedi: KESİNLİKLE paylaşma
- Finansal veri (maliyet, marj): KESİNLİKLE paylaşma
- Kapsam dışı sorular: kibarca müşteri hizmetlerine yönlendir
- Sorgulama adımları: müşteriye gösterme
- Varsayım: bilmediğin konularda ASLA yapma
"""

# ── SKU ayıkla ────────────────────────────────────────────────────────────────

_SKU_RE = re.compile(r'\b\d{10,13}\b')


def _extract_skus(question: str, explicit: Optional[str] = None) -> List[str]:
    found = _SKU_RE.findall(question)
    if explicit:
        found = [explicit] + found
    return list(dict.fromkeys(found))  # unique, order preserved


# ── DB'den PLM verisi ─────────────────────────────────────────────────────────

async def _fetch_db_products(
    session: AsyncSession,
    skus: List[str],
    question: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {}
    if skus:
        params["skus"] = skus
        cond = "urun_kodu = ANY(:skus)"
    else:
        keywords = [w for w in question.split() if len(w) >= 4][:3]
        if keywords:
            ks = [f"(urun_adi ILIKE :kw{i} OR tema_adi ILIKE :kw{i} OR urun_grubu_adi ILIKE :kw{i})"
                  for i in range(len(keywords))]
            cond = "(" + " OR ".join(ks) + ")"
            for i, kw in enumerate(keywords):
                params[f"kw{i}"] = f"%{kw}%"
        else:
            return []

    sql = text(f"""
        SELECT urun_kodu, urun_adi, marka_adi, sezon_adi, sezon_kodu,
               ana_grup_adi, urun_grubu_adi, tema_adi,
               fabricmaterialname, internet_aktif, bloke,
               color_codes, default_image_url
        FROM pim_products WHERE {cond}
        ORDER BY internet_aktif DESC NULLS LAST
        LIMIT :lim
    """)
    params["lim"] = limit
    rows = (await session.execute(sql, params)).mappings().all()
    return [dict(r) for r in rows]


# ── Ana servis ────────────────────────────────────────────────────────────────

async def run_callcenter(
    *,
    session: AsyncSession,
    question: str,
    urun_kodu: Optional[str] = None,
    custom_system: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Call Center Agent ana akışı."""
    started = time.perf_counter()
    skus = _extract_skus(question, urun_kodu)

    # DB + live paralel
    if skus:
        db_products, live_data = await _asyncio.gather(
            _fetch_db_products(session, skus, question),
            fetch_product_full(skus[0]),
        )
    else:
        db_products = await _fetch_db_products(session, skus, question)
        live_data = {}

    # İlk SKU'nun görsel URL'ini ekle
    img_url = None
    if db_products:
        img_url = db_products[0].get("default_image_url")
    if not img_url and live_data.get("details"):
        images = live_data["details"].get("productImages") or live_data["details"].get("images") or []
        if images:
            img_url = (images[0].get("imageUrl") or images[0].get("url") or "")

    context = {
        "sorgu_stok_kodu": skus[0] if skus else None,
        "gorsel_url": img_url,
        "db_product":    db_products[0] if db_products else None,
        "details":       live_data.get("details"),
        "stocks":        live_data.get("stocks") or [],
        "sales_prices":  live_data.get("sales_prices") or [],
        "erp_prices":    live_data.get("erp_prices") or [],
        "relations":     live_data.get("relations") or [],
        "size_values":   live_data.get("size_values") or [],
    }

    system = custom_system or CALL_CENTER_SYSTEM
    user_msg = (
        f"ÜRÜN VERİSİ (otomatik sorgulandı):\n"
        f"{json.dumps(context, default=str, ensure_ascii=False, indent=2)}\n\n"
        f"MÜŞTERİ / ÇAĞRI MERKEZİ SORUSU: {question}"
    )

    answer = await llm_client.complete(system=system, user=user_msg, temperature=0.3, history=history)

    return {
        "question":       question,
        "answer":         answer,
        "products_found": len(db_products),
        "elapsed_ms":     round((time.perf_counter() - started) * 1000, 1),
    }
