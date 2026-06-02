"""Sizewin Agent — Pimland moda ve beden danışmanı.

Çalışma prensibi:
  1. Ürün kodu/adından pim_products'ta kategori ve kumaş bilgisi çekilir
  2. Pimland MCP'den canlı: ürün detayı, beden ölçü tablosu, standart bedenler
  3. incorta_depo_iade'den iade oranı ve iade eğilimi hesaplanır
  4. Müşteri ölçüleri + tüm veri Claude'a iletilir
  5. Sistem prompt 4 bölümlü yanıt formatını yönetir
"""

from __future__ import annotations

import asyncio as _asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm_client import llm_client
from app.connectors.pimland_live import fetch_product_full
from app.core.logging import get_logger

log = get_logger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# SİSTEM PROMPT  (kullanıcı tarafından sağlandı)
# ══════════════════════════════════════════════════════════════════════════════

SIZEWIN_SYSTEM = """\
# ══════════════════════════════════════════════════
# ROL VE KİMLİK
# ══════════════════════════════════════════════════

Sen Pimland'ın deneyimli moda ve beden danışmanısın.
Kadın giyimde beden ölçülendirme uzmanlığınla müşterilerin
doğru bedeni bulmalarına yardımcı olurken onları kendileri
hakkında iyi hissettirirsin.

## Temel Kurallar
- Her zaman "Siz" ile hitap et
- Samimi, pozitif ve yönlendirici ton
- Beden, kilo veya vücut şekli hakkında ASLA olumsuz
  veya yargılayıcı ifade kullanma
- Teknik terimleri sade Türkçeyle açıkla
- Öneriyi kesin değil yaklaşık sun:
  "tam size göre" / "büyük ihtimalle uyacaktır"
- Arka planda yapılan veri sorgularını müşteriye gösterme

# ══════════════════════════════════════════════════
# VERİ — HAZIR SUNULACAK
# ══════════════════════════════════════════════════

Aşağıdaki veri bölümleri otomatik sorgulanmış ve
kullanıcı mesajına eklenmiştir:

• db_product     → PLM: marka, sezon, kategori, kumaş (fabricmaterialname)
• details        → MCP: ürün tanımı, kumaş kompozisyonu, kalıp bilgisi
• sizes          → MCP: standart beden tablosu (XS/S/M/L/XL ↔ göğüs/bel/kalça cm)
• size_values    → MCP: detay ölçüler (kol boyu, paça boyu, bel genişliği vb.)
• iade_analiz    → Incorta: bu ürünün iade oranı ve eğilimi

Birden fazla ölçü tipi varsa "internet" tipini önceliklendir.

# ══════════════════════════════════════════════════
# MÜŞTERİDEN BİLGİ TOPLAMA
# ══════════════════════════════════════════════════

Müşteri beden sormak istediğinde aşağıdaki bilgileri
TEK SEFERDE, sohbet havasında sor.
Tüm bilgileri vermek zorunda olmadığını belirt.

## Zorunlu
- Boy (cm)
- Kilo (kg)

## İsteğe Bağlı (daha isabetli öneri için)
- Göğüs çevresi (cm)
- Bel çevresi (cm)
- Kalça çevresi (cm)
- Genellikle tercih ettiği beden (XS/S/M/L/XL)

## Ürün Tipine Göre Ek Sorular
| Ürün Tipi                        | Ek Soru |
|----------------------------------|---------|
| Alt giyim (pantolon/etek/şort)   | Paça veya bel ölçüsü |
| Üst giyim (bluz/ceket/elbise)    | Omuz veya göğüs ölçüsü |
| Tüm ürünler                      | Tercih edilen fit: dar/normal/bol |

## Soru Şablonu
"Size en uygun bedeni önerebilmem için birkaç bilgiye
ihtiyacım var. Boy ve kilonuzu paylaşırsanız harika bir
başlangıç olur. Mümkünse göğüs, bel veya kalça ölçünüzü
de ekleyebilirsiniz — ama zorunlu değil. Genellikle hangi
bedeni tercih ettiğinizi de bilmek güzel olur."

# ══════════════════════════════════════════════════
# BEDEN HESAPLAMA MANTIĞI
# ══════════════════════════════════════════════════

## Adım 1 — Standart Tablo Karşılaştırması
Müşteri ölçülerini → standart beden tablosuyla eşleştir.

## Adım 2 — Detay Ölçü Eşleştirmesi
Ürün detay ölçüleri varsa (paça boyu, kol boyu vb.)
müşteri boyuyla karşılaştır.

## Adım 3 — Kumaş & Kalıp Yorumu
| Durum                        | Öneri Kuralı |
|------------------------------|--------------|
| Elastan / likra içeriyor     | Bir beden küçük önerilebilir |
| Oversize kalıp               | Bir beden küçük öner |
| Slim / dar kalıp             | Sınırda olan müşteriye bir büyük öner |
| Sert dokuma (keten/denim/kadife) | Ölçü tam olmalı, esnemez |

## Adım 4 — İki Beden Arasında Kalırsa
Müşterinin fit tercihine göre karar ver:
- Rahat/bol tercih → büyük beden
- Oturgan/dar tercih → küçük beden

# ══════════════════════════════════════════════════
# YANIT FORMATI — 4 BÖLÜM
# ══════════════════════════════════════════════════

Her beden önerisini bu sırayla ver:

**1. Öneri**
Önerilen bedeni ve varsa alternatifi belirt.

**2. Gerekçe**
Kısa ve sade. Kumaş veya kalıp etkisini mutlaka dahil et.

**3. Detay Ölçü Notu**
Ürüne özel ölçüler varsa müşteri için ne anlama geldiğini belirt.
(Örn: "Paça boyu 76 cm — 1,68 boyunuzda standart uzunluğa düşer.")

**4. Fit Notu**
Dar/normal/bol tercihine göre kısa yorum.

## Örnek Yanıt
"Bu ürün için M beden tam size göre olacak. Kumaşta elastan
bulunduğu için rahatça hareket edebilirsiniz; isterseniz S de
giyilebilir ama biraz daha oturgan bir görünüm olur. Paça boyu
76 cm olarak ölçülmüş, 1,68 boyunuzda standart uzunlukta
düşecektir."

# ══════════════════════════════════════════════════
# ÖZEL DURUMLAR
# ══════════════════════════════════════════════════

## İki Beden Arasında
"Ölçüleriniz M ve L arasında. Rahat kullanım için L,
daha oturgan görünüm için M daha uygun olacaktır."

## Sadece Boy/Kilo Verilmişse
Genel öneri yap ve şunu ekle:
"Göğüs veya bel ölçünüzü paylaşırsanız daha isabetli
bir öneri yapabilirim."

## Ürün Ölçüsü Yoksa
Standart tablo ve kumaş bilgisine dayanarak öner,
müşteriye bunu şeffafça belirt.

## İade Verisi Uyarıyorsa
Öneriyle birlikte şeffafça paylaş:
"Bu üründe iade oranı ortalamanın üzerinde; beden seçiminde
dikkatli olmanızı öneririm."

## Veri Eksikliği
- size_values gelmezse: "Ürün detay ölçülerine tam ulaşamıyorum;
  önerim genel tabloya dayanıyor, küçük sapmalar olabilir."
- Hesaplama için gerekli ölçü yoksa önce müşteriden iste.
"""

# ── SKU ayıkla ────────────────────────────────────────────────────────────────

_SKU_RE = re.compile(r'\b\d{10,13}\b')


def _extract_sku(question: str, explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    found = _SKU_RE.findall(question)
    return found[0] if found else None


# ── DB: PLM ürün verisi ───────────────────────────────────────────────────────

async def _fetch_db_product(
    session: AsyncSession,
    sku: Optional[str],
    urun_adi: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not sku and not urun_adi:
        return None
    params: Dict[str, Any] = {}
    if sku:
        cond = "urun_kodu = :uk"; params["uk"] = sku
    else:
        cond = "urun_adi ILIKE :ua"; params["ua"] = f"%{urun_adi}%"
    row = (await session.execute(text(f"""
        SELECT urun_kodu, urun_adi, marka_adi, sezon_adi,
               ana_grup_adi, urun_grubu_adi, fabricmaterialname,
               internet_aktif, bloke, default_image_url
        FROM pim_products WHERE {cond} LIMIT 1
    """), params)).mappings().first()
    return dict(row) if row else None


# ── DB: İade analizi ──────────────────────────────────────────────────────────

async def _fetch_iade_analiz(
    session: AsyncSession,
    sku: Optional[str],
) -> Dict[str, Any]:
    """Bu ürünün iade oranını ve eğilimini incorta verisiyle hesapla."""
    if not sku:
        return {}
    try:
        result = (await session.execute(text("""
            SELECT
                COALESCE(SUM(s.adet), 0)           AS satis_adet,
                COALESCE(ABS(SUM(d.adet)), 0)       AS iade_adet,
                COALESCE(SUM(s.tutar), 0)           AS brut_ciro,
                COALESCE(ABS(SUM(d.tutar)), 0)      AS iade_ciro
            FROM (
                SELECT urun_kodu, SUM(adet) AS adet, SUM(tutar) AS tutar
                FROM incorta_satis WHERE urun_kodu = :sku GROUP BY urun_kodu
            ) s
            FULL OUTER JOIN (
                SELECT urun_kodu, SUM(adet) AS adet, SUM(tutar) AS tutar
                FROM incorta_depo_iade WHERE urun_kodu = :sku GROUP BY urun_kodu
            ) d ON s.urun_kodu = d.urun_kodu
        """), {"sku": sku})).mappings().first()

        if not result:
            return {}

        satis = float(result["satis_adet"] or 0)
        iade  = float(result["iade_adet"]  or 0)
        brut  = float(result["brut_ciro"]  or 0)
        iade_ciro = float(result["iade_ciro"] or 0)
        iade_orani = round(iade / satis * 100, 1) if satis > 0 else 0

        # Yüksek iade uyarısı (>%25)
        uyari = None
        if iade_orani > 25:
            uyari = f"Bu üründe iade oranı %{iade_orani} — ortalamanın üzerinde. Beden kaynaklı olabilir, bir üst beden değerlendirilebilir."
        elif iade_orani > 15:
            uyari = f"İade oranı %{iade_orani} — biraz yüksek. Beden seçiminde dikkatli olunması önerilir."

        return {
            "satis_adet":   int(satis),
            "iade_adet":    int(iade),
            "iade_orani_pct": iade_orani,
            "uyari":        uyari,
        }
    except Exception as exc:
        log.warning("sizewin.iade_analiz_failed", sku=sku, error=str(exc))
        return {}


# ── Ana servis ────────────────────────────────────────────────────────────────

async def run_sizewin(
    *,
    session: AsyncSession,
    question: str,
    measurements: Optional[Dict[str, Any]] = None,
    urun_kodu: Optional[str] = None,
    urun_adi: Optional[str] = None,
    custom_system: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Sizewin Agent ana akışı."""
    started = time.perf_counter()
    sku = _extract_sku(question, urun_kodu)

    # Paralel veri çekme: DB (PLM + iade) + Pimland live
    if sku:
        db_product, iade_analiz, live_data = await _asyncio.gather(
            _fetch_db_product(session, sku, urun_adi),
            _fetch_iade_analiz(session, sku),
            fetch_product_full(sku),
        )
    else:
        db_product, iade_analiz = await _asyncio.gather(
            _fetch_db_product(session, None, urun_adi),
            _noop(),
        )
        live_data = {}

    # Beden tablosu fallback (MCP'den gelmezse yerel tablo)
    sizes_from_mcp = live_data.get("sizes") or []
    size_values_from_mcp = live_data.get("size_values") or []

    ana_grup = (db_product or {}).get("ana_grup_adi", "")
    fallback_chart = _fallback_size_chart(ana_grup) if not sizes_from_mcp else None

    context = {
        "musteri_olculeri":        measurements or {},
        "sorgu_stok_kodu":         sku,
        "db_product":              db_product,
        "details":                 live_data.get("details"),
        "sizes":                   sizes_from_mcp or fallback_chart,
        "size_values":             size_values_from_mcp,
        "iade_analiz":             iade_analiz,
        "beden_tablosu_kaynagi":   "MCP" if sizes_from_mcp else "Genel Fallback",
    }

    system = custom_system or SIZEWIN_SYSTEM
    user_msg = (
        f"ÜRÜN VE BEDEN VERİSİ (otomatik sorgulandı):\n"
        f"{json.dumps(context, default=str, ensure_ascii=False, indent=2)}\n\n"
        f"MÜŞTERİ SORUSU: {question}"
    )

    answer = await llm_client.complete(system=system, user=user_msg, temperature=0.35, history=history)

    return {
        "question":       question,
        "answer":         answer,
        "product":        db_product,
        "size_chart_used": "MCP Ürün Tablosu" if sizes_from_mcp else f"Genel Tablo ({ana_grup or 'Varsayılan'})",
        "elapsed_ms":     round((time.perf_counter() - started) * 1000, 1),
    }


# ── Yedek beden tabloları (MCP'den gelmezse) ─────────────────────────────────

def _fallback_size_chart(ana_grup: str) -> List[Dict]:
    charts = {
        "ALT GRUP": [
            {"beden": "34", "bel_cm": "60-62", "kalca_cm": "84-86"},
            {"beden": "36", "bel_cm": "62-65", "kalca_cm": "86-90"},
            {"beden": "38", "bel_cm": "65-68", "kalca_cm": "90-94"},
            {"beden": "40", "bel_cm": "68-72", "kalca_cm": "94-98"},
            {"beden": "42", "bel_cm": "72-76", "kalca_cm": "98-102"},
            {"beden": "44", "bel_cm": "76-80", "kalca_cm": "102-106"},
            {"beden": "46", "bel_cm": "80-86", "kalca_cm": "106-112"},
        ],
        "ELBISE": [
            {"beden": "XS/34", "gogus_cm": "80-84", "bel_cm": "60-64", "kalca_cm": "86-90"},
            {"beden": "S/36",  "gogus_cm": "84-88", "bel_cm": "64-68", "kalca_cm": "90-94"},
            {"beden": "M/38",  "gogus_cm": "88-92", "bel_cm": "68-72", "kalca_cm": "94-98"},
            {"beden": "L/40",  "gogus_cm": "92-96", "bel_cm": "72-76", "kalca_cm": "98-102"},
            {"beden": "XL/42", "gogus_cm": "96-100","bel_cm": "76-82", "kalca_cm": "102-108"},
            {"beden": "XXL/44","gogus_cm": "100-108","bel_cm":"82-90", "kalca_cm": "108-116"},
        ],
        "UST DIS GIYIM": [
            {"beden": "XS", "gogus_cm": "82-86", "omuz_cm": "36-37"},
            {"beden": "S",  "gogus_cm": "86-90", "omuz_cm": "37-38"},
            {"beden": "M",  "gogus_cm": "90-94", "omuz_cm": "38-40"},
            {"beden": "L",  "gogus_cm": "94-98", "omuz_cm": "40-42"},
            {"beden": "XL", "gogus_cm": "98-104","omuz_cm": "42-44"},
            {"beden": "XXL","gogus_cm": "104-112","omuz_cm":"44-46"},
        ],
    }
    default = [
        {"beden": "XS", "gogus_cm": "80-84", "bel_cm": "60-64", "kalca_cm": "86-90",  "boy_onerisi": "155-162"},
        {"beden": "S",  "gogus_cm": "84-88", "bel_cm": "64-68", "kalca_cm": "90-94",  "boy_onerisi": "158-165"},
        {"beden": "M",  "gogus_cm": "88-92", "bel_cm": "68-72", "kalca_cm": "94-98",  "boy_onerisi": "163-170"},
        {"beden": "L",  "gogus_cm": "92-96", "bel_cm": "72-76", "kalca_cm": "98-102", "boy_onerisi": "165-172"},
        {"beden": "XL", "gogus_cm": "96-100","bel_cm": "76-82", "kalca_cm": "102-108","boy_onerisi": "167-175"},
        {"beden": "XXL","gogus_cm": "100-108","bel_cm":"82-90", "kalca_cm": "108-116","boy_onerisi": "168-176"},
    ]
    return charts.get((ana_grup or "").upper(), default)


async def _noop() -> Dict:
    return {}
