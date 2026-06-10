"""
Ürün Görsel Analiz Servisi
Claude Vision (Bedrock) ile ürün görsellerinden özellik çıkarır.
Mevcut Pimland verisiyle karşılaştırıp hata ve eksik bulur.
"""
import boto3
import json
import base64
import asyncio
import os
import httpx
from typing import List, Optional

CDN_BASE = "https://img-adl.sm.mncdn.com/mnresize/1500/2000/pimages"
CDN_PRODUCTS = "https://img-adl.sm.mncdn.com/cdnimages/products"

# Proje geneli ile aynı model — env'den oku, yoksa Sonnet 4.6 eu-north-1
CLAUDE_MODEL = os.environ.get(
    "BEDROCK_MODEL_ID",
    "arn:aws:bedrock:eu-north-1:448049806345:inference-profile/eu.anthropic.claude-sonnet-4-6"
)
BEDROCK_REGION = "eu-north-1"


def get_image_urls(product: dict, max_images: int = 3) -> list[str]:
    """Pimland ürününden CDN görsel URL'leri üretir (MCP productImages)."""
    images = product.get("productImages", [])
    urls = []
    for img in images:
        if len(urls) >= max_images:
            break
        if not img.get("isDeleted") and img.get("name"):
            urls.append(f"{CDN_BASE}/{img['name']}")
    return urls


def get_image_urls_from_colors(stock_code: str, colors: List[str], max_per_color: int = 3) -> List[str]:
    """DB color_codes'dan cdnimages CDN URL'leri üretir."""
    urls = []
    for color in colors[:2]:
        for n in range(1, max_per_color + 1):
            urls.append(f"{CDN_PRODUCTS}/{stock_code}_{color}_{n}.jpg")
    return urls


async def fetch_image_as_base64(url: str) -> Optional[str]:
    """CDN'den görsel indir, base64 string döndür."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return base64.standard_b64encode(r.content).decode()
    except Exception as e:
        print(f"Görsel indirme hatası [{url}]: {e}")
    return None


def extract_pimland_fields(product: dict) -> dict:
    """Karşılaştırılacak tüm PLM alanlarını çıkarır."""
    return {
        "description":        product.get("description"),
        "productGroupName":   product.get("productGroupName"),
        "productTypeName":    product.get("productTypeName"),
        "fabricPatternName":  product.get("fabricPatternName"),
        "fabricMaterialName": product.get("fabricMaterialName"),
        "compositionName":    product.get("compositionName"),
        "thicknessTypeName":  product.get("thicknessTypeName"),
        "armLengthName":      product.get("armLengthName"),
        "collarTypeName":     product.get("collarTypeName"),
        "productLengthName":  product.get("productLengthName"),
        "cuttingName":        product.get("cuttingName"),
        "beltLengthName":     product.get("beltLengthName"),
        "fitName":            product.get("fitName"),
        "styleName":          product.get("styleName"),
        "ecomTag1Name":       product.get("ecomTag1Name"),
        "ecomTag2Name":       product.get("ecomTag2Name"),
        "ecomTag3Name":       product.get("ecomTag3Name"),
        "ecomTag4Name":       product.get("ecomTag4Name"),
    }


VISION_SYSTEM_PROMPT = """Sen bir moda PLM uzmanısın.
Ürün fotoğraflarına bakarak PLM sistemi için özellik çıkarırsın.

KURAL: SADECE GÖRDÜĞÜN ŞEYİ YAZ. Emin değilsen "belirsiz" yaz.

ÇIKARILACAK ÖZELLİKLER:

DESEN (fabricPatternName):
  Düz | Çiçek Desen | Çizgili | Kareli | Puantiyeli | Şal Desen |
  Geometrik | Ekose | Batik | Leopar | Zebra | Yaprak Desen |
  Etnik Desen | Baskılı | Karışık Desen | File | Ajur

KOL BOYU (armLengthName):
  Kolsuz | Askılı | Kısa Kol | Yarım Kol | 3/4 Kol | Uzun Kol

YAKA TİPİ (collarTypeName):
  V Yaka | Derin V Yaka | Yuvarlak Yaka | U Yaka | Kare Yaka |
  Kayık Yaka | Balıkçı Yaka | Polo Yaka | Halter Yaka |
  Asimetrik Yaka | Bant Yaka | Kapalı Yaka | Bağcıklı Yaka

ÜRÜN UZUNLUĞU (productLengthName):
  Mini | Kısa | Midi | Uzun | Maxi

KESİM (cuttingName):
  Dar Kesim | Normal Kesim | Oversize | Geniş Kesim | A Kesim | Crop

BEL YÜKSEKLİĞİ (beltLengthName):
  Yüksek Bel | Orta Bel | Düşük Bel
  (pantolon/etek/şort dışında "uygulanamaz" yaz)

KALIP (fitName) — PLM'de çoğunlukla boş:
  Slim | Regular | Oversize | Relaxed | Fitted | Loose | Crop

KALINLIK (thicknessTypeName) — PLM'de çoğunlukla boş:
  İnce | Orta | Kalın

STİL (styleName):
  Günlük | Spor | Şık | Plaj | Klasik | Parti | Ofis | Casual | Gece

E-TİCARET ETİKETİ 3 (ecomTag3Name) — PLM'de varsa karşılaştır, yoksa öner:
  Fermuarlı | Düğmeli | Bağlamalı | Dantel | Güpür | Pile | Büzgü |
  Yırtmaçlı | Fırfırlı | Nakışlı | Transparan | File Detaylı |
  Şeritli | Payet | Cepli | Volanlı | Drapeli | Cut-Out | Balonlu |
  Sırt Detaylı | Yaka Detaylı | Kapitone | Kemer Detaylı | Düz | Baskılı

E-TİCARET ETİKETİ 4 (ecomTag4Name) — PLM'de varsa karşılaştır, yoksa öner:
  (yukarıdaki listeden farklı bir özellik seç)

YANIT — SADECE JSON:
{
  "cikarilan": {
    "fabricPatternName":  "...",
    "armLengthName":      "...",
    "collarTypeName":     "...",
    "productLengthName":  "...",
    "cuttingName":        "... veya uygulanamaz",
    "beltLengthName":     "... veya uygulanamaz",
    "fitName":            "...",
    "thicknessTypeName":  "...",
    "styleName":          "...",
    "ecomTag3Name":       "... veya null",
    "ecomTag4Name":       "... veya null"
  },
  "guven_skoru": 0.0,
  "notlar": "..."
}"""


async def analyze_product_images(
    product: dict,
    bedrock_client=None,
    image_urls: Optional[List[str]] = None,
) -> dict:
    """Ana analiz fonksiyonu — görsel → özellik çıkarımı + PLM karşılaştırması.

    image_urls: MCP productImages yerine doğrudan URL listesi geçilebilir.
    """
    stock_code = product.get("stockCode", "bilinmiyor")

    if not bedrock_client:
        bedrock_client = boto3.client(
            "bedrock-runtime", region_name=BEDROCK_REGION
        )

    if image_urls is None:
        image_urls = get_image_urls(product, max_images=3)
    if not image_urls:
        return {"stock_code": stock_code, "hata": "Görsel bulunamadı"}

    images_b64 = []
    for url in image_urls:
        b64 = await fetch_image_as_base64(url)
        if b64:
            images_b64.append(b64)

    if not images_b64:
        return {"stock_code": stock_code, "hata": "Görsel indirilemedi"}

    mevcut = extract_pimland_fields(product)

    content = []
    for b64 in images_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })

    content.append({
        "type": "text",
        "text": f"""Ürün: {mevcut['description']} ({mevcut['productGroupName']})
Ürün Tipi: {mevcut['productTypeName']}

Mevcut PLM değerleri (null = boş, doldurmaya çalış):
{json.dumps(mevcut, ensure_ascii=False, indent=2)}

Görselden tüm özellikleri çıkar.
Özellikle NULL olan alanları doldurmaya çalış:
fitName, thicknessTypeName, ecomTag3Name, ecomTag4Name
Mevcut değerlerle çelişen varsa görselden gördüğün değeri yaz."""
    })

    response = bedrock_client.invoke_model(
        modelId=CLAUDE_MODEL,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1500,
            "system": VISION_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1
        }),
        contentType="application/json",
        accept="application/json"
    )

    result = json.loads(response["body"].read())
    text = result["content"][0]["text"].strip()

    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    vision_data = json.loads(text)
    cikarilan = vision_data.get("cikarilan", {})

    karsilastirma = compare_with_pimland(mevcut, cikarilan)

    return {
        "stock_code":      stock_code,
        "cikarilan":       cikarilan,
        "uyusmazlik":      karsilastirma["uyusmazlik"],
        "eksik":           karsilastirma["eksik"],
        "oneri":           karsilastirma["oneri"],
        "ozet":            karsilastirma["ozet"],
        "guven_skoru":     vision_data.get("guven_skoru", 0.0),
        "notlar":          vision_data.get("notlar", ""),
        "gorsel_sayisi":   len(images_b64),
        "mevcut_pimland":  mevcut
    }


def compare_with_pimland(mevcut: dict, cikarilan: dict) -> dict:
    """Görsel çıkarımını PLM verisiyle karşılaştırır."""

    MAPPING = {
        "fabricPatternName":  "fabricPatternName",
        "armLengthName":      "armLengthName",
        "collarTypeName":     "collarTypeName",
        "productLengthName":  "productLengthName",
        "cuttingName":        "cuttingName",
        "beltLengthName":     "beltLengthName",
        "fitName":            "fitName",
        "thicknessTypeName":  "thicknessTypeName",
        "styleName":          "styleName",
        "ecomTag3Name":       "ecomTag3Name",
        "ecomTag4Name":       "ecomTag4Name",
    }

    YUKSEK_ONCELIK = {
        "fabricPatternName", "armLengthName",
        "productLengthName", "collarTypeName"
    }

    uyusmazlik = []
    eksik = []
    oneri = {}

    for pimland_field, vision_field in MAPPING.items():
        pimland_deger = mevcut.get(pimland_field)
        vision_deger = cikarilan.get(vision_field)

        if not vision_deger or vision_deger in ("belirsiz", "uygulanamaz", "null", None):
            continue

        if not pimland_deger:
            eksik.append({
                "alan":          pimland_field,
                "gorsel_degeri": vision_deger,
                "oncelik":       "yuksek" if pimland_field in YUKSEK_ONCELIK else "orta",
                "aciklama":      f"PLM'de boş, görselde '{vision_deger}'"
            })
            oneri[pimland_field] = vision_deger
        elif pimland_deger.lower().strip() != vision_deger.lower().strip():
            uyusmazlik.append({
                "alan":           pimland_field,
                "pimland_degeri": pimland_deger,
                "gorsel_degeri":  vision_deger,
                "risk":           "yuksek" if pimland_field in YUKSEK_ONCELIK else "orta",
                "aciklama":       f"PLM '{pimland_deger}' ↔ Görsel '{vision_deger}'"
            })
            oneri[pimland_field] = vision_deger

    return {
        "uyusmazlik": uyusmazlik,
        "eksik":      eksik,
        "oneri":      oneri,
        "ozet": {
            "toplam_uyusmazlik":         len(uyusmazlik),
            "toplam_eksik":              len(eksik),
            "oneri_alan_sayisi":         len(oneri),
            "yuksek_riskli_alanlar":     [u["alan"] for u in uyusmazlik if u.get("risk") == "yuksek"],
            "yuksek_oncelikli_eksikler": [e["alan"] for e in eksik if e.get("oncelik") == "yuksek"]
        }
    }


async def batch_analyze(
    products: list[dict],
    bedrock_client=None,
    delay_seconds: float = 0.5
) -> list[dict]:
    """Birden fazla ürünü sıralı analiz eder."""
    results = []
    for i, product in enumerate(products):
        print(f"[{i+1}/{len(products)}] {product.get('stockCode')} analiz ediliyor...")
        try:
            result = await analyze_product_images(product, bedrock_client)
            results.append(result)
        except Exception as e:
            results.append({"stock_code": product.get("stockCode"), "hata": str(e)})
        if i < len(products) - 1:
            await asyncio.sleep(delay_seconds)
    return results
