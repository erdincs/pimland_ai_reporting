"""Curated DB schema catalogue fed to the text-to-SQL prompt.

The agent never sees raw data — only this catalogue. Keep it:
  * accurate (in sync with real table/column names),
  * minimal (no redundant columns the agent doesn't need),
  * business-friendly (Turkish descriptions, concrete value examples).

Sections:
  1. Incorta — e-ticaret satış ve iade verileri
  2. Pimland PLM — master data (markalar, renkler, sezonlar, gruplar)
  3. Pre-aggregated views (ALWAYS prefer these for summary/trend queries)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: list
    is_view: bool = False


# ── 1. Incorta — E-ticaret işlem verileri ────────────────────────────────────

INCORTA_TABLES: list = [
    Table(
        name="incorta_satis",
        description=(
            "Incorta'dan çekilen e-ticaret brüt satış işlemleri. "
            "Her satır: bir ay + kanal + SKU + renk + beden kombinasyonunun toplam ciro ve adeti. "
            "108K+ satır, 2026 YTD."
        ),
        columns=[
            Column("yil",          "integer", "Yıl (örn: 2026)."),
            Column("ay",           "integer", "Ay (1=Ocak … 12=Aralık)."),
            Column("satis_kanali", "text",
                   "Satış kanalı. Değerler: TRENDYOL, ADL, 'ADL IOS APP', 'ADL ANDROID APP', "
                   "HEPSIBURADA, BOYNER, LOVEMYBODY, 'TY ADL AZ', 'LMB IOS APP', 'LMB ANDROID APP', 'TY LMB AZ'."),
            Column("urun_kodu",    "text",    "Ürün/SKU kodu. Örn: 10146095000."),
            Column("urun_adi",     "text",    "Ürün adı. Örn: Triko Atkı."),
            Column("renk",         "text",    "Renk. Örn: Antrasit, Kemik, Ekru."),
            Column("beden",        "text",    "Beden. Örn: XS, S, M, L, XL, XXL, 36-50."),
            Column("tutar",        "double precision", "Brüt ciro (TL). Her zaman pozitif."),
            Column("adet",         "double precision", "Satış adedi."),
        ],
    ),
    Table(
        name="incorta_iptal_siparis",
        description=(
            "İptal edilen sipariş verileri. "
            "Aynı yapı: yil, ay, satis_kanali, urun_kodu, urun_adi, renk, beden. "
            "tutar ve adet NEGATIF — iade/iptal tutarlarıdır. 10K+ satır."
        ),
        columns=[
            Column("yil",          "integer", "Yıl."),
            Column("ay",           "integer", "Ay (1-12)."),
            Column("satis_kanali", "text",    "Satış kanalı."),
            Column("urun_kodu",    "text",    "Ürün kodu."),
            Column("urun_adi",     "text",    "Ürün adı."),
            Column("renk",         "text",    "Renk."),
            Column("beden",        "text",    "Beden."),
            Column("tutar",        "double precision", "İptal tutarı (negatif TL)."),
            Column("adet",         "double precision", "İptal adedi (negatif)."),
        ],
    ),
    Table(
        name="incorta_depo_iade",
        description=(
            "Depoya dönen iade verileri. "
            "incorta_iptal_siparis ile aynı yapı. tutar ve adet NEGATIF. 45K+ satır."
        ),
        columns=[
            Column("yil",          "integer", "Yıl."),
            Column("ay",           "integer", "Ay (1-12)."),
            Column("satis_kanali", "text",    "Satış kanalı."),
            Column("urun_kodu",    "text",    "Ürün kodu."),
            Column("urun_adi",     "text",    "Ürün adı."),
            Column("renk",         "text",    "Renk."),
            Column("beden",        "text",    "Beden."),
            Column("tutar",        "double precision", "İade tutarı (negatif TL)."),
            Column("adet",         "double precision", "İade adedi (negatif)."),
        ],
    ),
    Table(
        name="incorta_analytics",
        description=(
            "Web analytics verisi (Incorta dashboard). "
            "Günlük, marka+kaynak+kampanya bazında trafik ve dönüşüm metrikleri. 24K+ satır."
        ),
        columns=[
            Column("date",                   "date",   "Tarih (günlük granülarite)."),
            Column("marka",                  "text",   "Marka. Değerler: ADL, LMB vb."),
            Column("oturum_kaynagi",         "text",   "Trafik kaynağı. Örn: google, Instagram_Feed, direct."),
            Column("oturum_kampanyasi",      "text",   "Kampanya adı. Örn: INB_Conversion_Interest."),
            Column("kullanicilar",           "bigint", "Benzersiz kullanıcı sayısı."),
            Column("oturumlar",              "bigint", "Oturum sayısı."),
            Column("ciro",                   "double precision", "O günün e-ticaret cirosu (TL)."),
            Column("islem_sayisi",           "bigint", "Tamamlanan işlem/sipariş sayısı."),
            Column("conversion_rate",        "double precision", "Dönüşüm oranı (0-1 arasında)."),
            Column("hemen_cikma_orani",      "double precision", "Hemen çıkma oranı (0-1)."),
            Column("ortalama_oturum_suresi", "double precision", "Ortalama oturum süresi (saniye)."),
        ],
    ),
]

# ── 2. Pimland PLM — Master data ─────────────────────────────────────────────

PLM_TABLES: list = [
    Table(
        name="pim_brands",
        description="PLM marka tablosu. adL, Love My Body, Night Zoom.",
        columns=[
            Column("id",             "bigint", "Birincil anahtar."),
            Column("name",           "text",   "Marka adı. Değerler: adL, Love My Body, Night Zoom."),
            Column("reference_code", "text",   "ERP referans kodu. Örn: 01 (ADL), LMB."),
            Column("is_active",      "boolean","Aktif mi?"),
        ],
    ),
    Table(
        name="pim_colors",
        description="PLM renk master data. 241 renk, Türkçe + İngilizce isimler.",
        columns=[
            Column("id",             "bigint", "Birincil anahtar."),
            Column("name",           "text",   "Renk adı (Türkçe). Örn: Antrasit, Kemik, Ekru."),
            Column("reference_code", "text",   "ERP renk kodu."),
            Column("is_active",      "boolean","Aktif mi?"),
            Column("translations",   "text",   "JSON: [{language, name}] çeviri listesi."),
        ],
    ),
    Table(
        name="pim_product_groups",
        description="PLM ürün grupları (59 grup). Ürün kategorilendirmesi.",
        columns=[
            Column("id",             "bigint", "Birincil anahtar."),
            Column("name",           "text",   "Grup adı."),
            Column("reference_code", "text",   "ERP grup kodu."),
            Column("is_active",      "boolean","Aktif mi?"),
        ],
    ),
    Table(
        name="pim_seasons",
        description="PLM sezon tablosu (35 sezon). 2018 AUTUMN'dan günümüze.",
        columns=[
            Column("id",             "bigint", "Birincil anahtar."),
            Column("name",           "text",   "Sezon adı. Örn: 2026 SPRING, 2025 AUTUMN."),
            Column("reference_code", "text",   "Sezon kodu. Örn: 26-SG, 25-AN."),
            Column("is_active",      "boolean","Aktif mi?"),
        ],
    ),
]

# ── 3. Pre-aggregated materialized views (ALWAYS PREFER) ─────────────────────

MATERIALIZED_VIEWS: list = [
    Table(
        name="mv_satis_aylik",
        description=(
            "TERCIH ET: Aylık toplam ciro ve adet — kanal + ay bazında önceden hesaplanmış. "
            "Trend, karşılaştırma ve özet sorgular için incorta_satis'ten çok daha hızlı."
        ),
        is_view=True,
        columns=[
            Column("yil",          "integer", "Yıl."),
            Column("ay",           "integer", "Ay (1-12)."),
            Column("satiskanali",  "text",    "Satış kanalı."),
            Column("toplam_ciro",  "numeric", "Ay + kanal toplam ciro (TL)."),
            Column("toplam_adet",  "bigint",  "Toplam adet."),
        ],
    ),
    Table(
        name="mv_satis_urun",
        description=(
            "TERCIH ET: Ürün bazında toplam ciro, adet ve kanal sayısı. "
            "'En çok satan ürünler', 'Top 10 SKU' için kullan."
        ),
        is_view=True,
        columns=[
            Column("itemcode",     "text",    "Ürün kodu (SKU)."),
            Column("item",         "text",    "Ürün adı."),
            Column("toplam_ciro",  "numeric", "Toplam ciro (TL)."),
            Column("toplam_adet",  "bigint",  "Toplam adet."),
            Column("kanal_sayisi", "bigint",  "Kaç farklı kanalda satıldığı."),
        ],
    ),
    Table(
        name="mv_satis_kanal",
        description=(
            "TERCIH ET: Kanal bazında toplam ciro ve pazar payı. "
            "'Hangi kanal en çok sattı?' için kullan."
        ),
        is_view=True,
        columns=[
            Column("satiskanali",  "text",    "Satış kanalı."),
            Column("toplam_ciro",  "numeric", "Toplam ciro (TL)."),
            Column("toplam_adet",  "bigint",  "Toplam adet."),
            Column("pazar_payi",   "numeric", "Kanalın toplam ciro içindeki % payı."),
        ],
    ),
]

# ── Full catalogue ────────────────────────────────────────────────────────────

CATALOGUE: list = MATERIALIZED_VIEWS + INCORTA_TABLES + PLM_TABLES


def render_schema_prompt(catalogue: list = CATALOGUE) -> str:
    """Render the catalogue as compact DDL-ish text for the system prompt."""
    lines: list = []
    for table in catalogue:
        kind = "MATERIALIZED VIEW" if table.is_view else "TABLE"
        lines.append(f"-- {table.description}")
        lines.append(f"{kind} {table.name} (")
        for col in table.columns:
            comment = f"  -- {col.description}" if col.description else ""
            lines.append(f"    {col.name} {col.type},{comment}")
        lines.append(");\n")
    return "\n".join(lines)
