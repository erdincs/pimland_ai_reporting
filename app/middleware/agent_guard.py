"""
app/middleware/agent_guard.py

Pimland Call Center & Sizewin Agent — 5 Katmanlı Güvenlik Middleware
Kullanım: main.py'de app.add_middleware(AgentGuardMiddleware) ile ekle
"""

import re
import json
import logging
import uuid
from datetime import datetime
from typing import Callable
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger("agent_guard")

# ══════════════════════════════════════════════════
# KATMAN 1 — INPUT GUARD
# ══════════════════════════════════════════════════

# Prompt injection ve jailbreak pattern'leri
INJECTION_PATTERNS = [
    r"ignore\s+(previous|all|prior)\s+instructions?",
    r"forget\s+(your|all|previous)\s+(instructions?|rules?|prompt)",
    r"you\s+are\s+now\s+a",
    r"pretend\s+(you\s+are|to\s+be)",
    r"act\s+as\s+(if\s+you\s+are|a\s+different)",
    r"(sistem|system)\s*(prompt(u|unu|unuzu)?|talimat)",
    r"(önceki|previous)\s+talimatlar[ıi]",
    r"(sen|you)\s+aslında",
    r"rol\s+(yap[a-z]*|üstlen)",
    r"(nasıl|how)\s+(çalışıyorsun|do\s+you\s+work)",
    r"(ne\s+tür|what\s+kind\s+of)\s+(yazılım|software|model|ai)",
    r"(api|endpoint|database|veritaban[ıi])\s+(nedir|göster|ver)",
    r"DAN\s+mode",
    r"jailbreak",
    r"<\s*script",       # XSS denemesi
    r"prompt\s+inject",
]

# Kapsam dışı konular — bu kategorilerde sabit yönlendirme
KAPSAM_DISI = {
    "odeme": [
        r"ödeme", r"kredi\s+kart", r"havale", r"eft",
        r"taksit", r"pos", r"iyzico", r"stripe"
    ],
    "teslimat": [
        r"kargo", r"teslimat", r"gönderi", r"paket",
        r"takip\s+no", r"desi", r"kurye"
    ],
    "iade_sureci": [
        r"iade\s+(süreci|nasıl|formu|kodu)",
        r"para\s+iad", r"geri\s+ödem"
    ],
    "teknik": [
        r"python", r"javascript", r"sql", r"kod\s+yaz",
        r"postgresql", r"redis", r"docker", r"aws",
        r"sunucu", r"server", r"yazılım\s+nasıl"
    ],
    "finans": [
        r"maliyet", r"kar\s+marj", r"tedarikçi\s+fiyat",
        r"alış\s+fiyat", r"kârlılık"
    ],
    "rakip": [
        r"rakip", r"zara", r"h&m", r"lcw", r"koton",
        r"mango", r"other\s+brand"
    ],
}

# Yönlendirme mesajları
YONLENDIRME = {
    "injection":    "Bu konuda size yardımcı olamıyorum. Ürünlerimiz hakkında bir sorunuz var mı?",
    "odeme":        "Ödeme ve fatura konularında müşteri hizmetleri ekibimiz size yardımcı olacaktır.",
    "teslimat":     "Kargo ve teslimat konularında müşteri hizmetleri ekibimize başvurabilirsiniz.",
    "iade_sureci":  "İade işlemleri için müşteri hizmetleri ekibimiz size yardımcı olacaktır.",
    "teknik":       "Bu konuda size yardımcı olamıyorum. Ürünlerimiz hakkında yardımcı olmamı ister misiniz?",
    "finans":       "Fiyat bilgisi dışındaki finansal detaylar için müşteri hizmetleri ekibimize yönlendiriyorum.",
    "rakip":        "Yalnızca kendi ürünlerimiz hakkında bilgi verebiliyorum.",
}


def input_guard(mesaj: str) -> tuple[bool, str]:
    """
    Kullanıcı mesajını giriş filtresinden geçir.
    Döner: (geçerli_mi, red_nedeni)
    geçerli_mi=True → LLM'e ilet
    geçerli_mi=False → red_nedeni ile yanıt dön
    """
    m = mesaj.lower()

    # Injection kontrolü
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, m, re.IGNORECASE):
            logger.warning(f"[INPUT_GUARD] Injection tespit edildi: {pattern[:40]}")
            return False, "injection"

    # Kapsam dışı konu kontrolü
    for kategori, patterns in KAPSAM_DISI.items():
        for pattern in patterns:
            if re.search(pattern, m, re.IGNORECASE):
                logger.info(f"[INPUT_GUARD] Kapsam dışı konu: {kategori}")
                return False, kategori

    return True, "ok"


# ══════════════════════════════════════════════════
# KATMAN 3 — FINANCIAL DATA FIELD FİLTRESİ
# ══════════════════════════════════════════════════
# Kural listesi artık sync/config/agent_tools.yaml'dan okunur.
# Yeni alan eklemek: YAML'daki field_filter.block listesini güncelle.

try:
    from sync.config_loader import get_blocked_fields as _get_blocked_fields
    _CONFIG_LOADED = True
except ImportError:
    _CONFIG_LOADED = False

# Fallback — config yoksa (test ortamı) bu sabit set kullanılır
_FALLBACK_YASAK = {
    "calculatedsalesprice", "markup", "barecostofgoods", "costsummary",
    "actualmarkup", "realsellingpriceInturkishlira", "realsellingpriceindolar",
    "costsummaryitem", "revisioncode", "revisiondescription",
    "cost", "unitcost", "productioncost", "purchaseprice", "supplierprice",
    "costbreakdown", "profitmargin", "margin", "grossprofit",
    "targetcost", "actualcost", "budgetcost", "costperunit",
    "totalcost", "manufacturingcost",
}


def _blocked_for_agent(agent: str) -> set:
    if _CONFIG_LOADED:
        try:
            return _get_blocked_fields(agent)
        except Exception:
            pass
    return _FALLBACK_YASAK


def financial_field_filter(data: dict, agent: str = "callcenter") -> dict:
    """
    get_product_financial_datas yanıtından finansal kırılım alanlarını filtrele.
    Blocked alan listesi agent_tools.yaml'dan okunur.
    Nested dict/list içindeki finansal değerler de temizlenir.
    """
    if not isinstance(data, dict):
        return data

    yasak = _blocked_for_agent(agent)

    temizlendi = {}
    for key, value in data.items():
        key_lower = key.lower()

        # Tam eşleşme
        if key_lower in yasak:
            logger.debug(f"[FIELD_FILTER] Finansal alan filtrelendi: {key}")
            continue

        # Kısmi eşleşme (≥4 karakter yasak kelime içeriyorsa)
        if any(y in key_lower for y in yasak if len(y) >= 4):
            logger.debug(f"[FIELD_FILTER] Finansal alan filtrelendi (kısmi): {key}")
            continue

        # Nested temizleme
        if isinstance(value, list):
            temizlendi[key] = [
                financial_field_filter(item, agent) if isinstance(item, dict) else item
                for item in value
            ]
        elif isinstance(value, dict):
            temizlendi[key] = financial_field_filter(value, agent)
        else:
            temizlendi[key] = value

    return temizlendi


# ══════════════════════════════════════════════════
# KATMAN 4 — OUTPUT GUARD
# ══════════════════════════════════════════════════

# Yanıtta bulunmaması gereken pattern'ler
CIKIS_YASAK_PATTERNS = [
    # Stok adedi sızıntısı
    (r"\b\d+\s*(adet|piece|stock|unit)\s*(var|kaldı|mevcut|bulunuyor)",
     "stok_adedi"),

    # Finansal bilgi sızıntısı
    (r"\b(maliyet|birim\s+maliyet|üretim\s+maliyeti)\s*:?\s*[\d.,]+",
     "maliyet"),
    (r"\b(kar\s+marj[ıi]|kârlılık|profit\s+margin)\s*:?\s*%?[\d.,]+",
     "kar_marji"),
    (r"\b(tedarikçi|supplier)\s+fiyat[ıi]\s*:?\s*[\d.,]+",
     "tedarikci_fiyat"),
    (r"\balış\s+fiyat[ıi]\s*:?\s*[\d.,]+",
     "alis_fiyat"),

    # Teknik bilgi sızıntısı
    (r"\b(postgresql|redis|fastapi|sqlalchemy|python\s+\d)\b",
     "teknik_altyapi"),
    (r"\b(api[_\s]key|token|bearer|authorization)\s*[=:]\s*\S+",
     "api_key"),
    (r"\b(traceback|stack\s+trace|exception|error\s+at\s+line)\b",
     "hata_mesaji"),

    # Sistem prompt sızıntısı
    (r"\b(system\s+prompt|talimatlar[ıi]m|kurallar[ıi]m|rol[üu]m)\b",
     "prompt_sizintisi"),

    # Stok sayısı (rakam + stok kelimesi)
    (r"stok(ta|umuzda)?\s+\d+",
     "stok_adedi"),
]

# Güvenli yönlendirme mesajı
GUVENLI_YONLENDIRME = (
    "Bu konuda müşteri hizmetleri ekibimiz size daha iyi yardımcı olabilir. "
    "Size başka nasıl yardımcı olabilirim?"
)


def output_guard(yanit: str) -> tuple[str, bool, str]:
    """
    LLM yanıtını çıkış filtresinden geçir.
    Döner: (temiz_yanit, mudahale_edildi_mi, neden)
    """
    for pattern, neden in CIKIS_YASAK_PATTERNS:
        if re.search(pattern, yanit, re.IGNORECASE):
            logger.warning(f"[OUTPUT_GUARD] Sızıntı tespit edildi: {neden}")

            # Tam yanıtı blokla, güvenli mesaj döndür
            return GUVENLI_YONLENDIRME, True, neden

    return yanit, False, "ok"


# ══════════════════════════════════════════════════
# KATMAN 5 — LOGLAMA & ANOMALİ TESPİTİ
# ══════════════════════════════════════════════════

# Oturum başına kapsam dışı sorgu sayacı
# Production'da Redis'e taşı
_oturum_sayac: dict[str, int] = {}
ANOMALI_ESIGI = 3       # Bu kadar kapsam dışı sorgu → uyarı
BLOK_ESIGI = 5          # Bu kadar → oturumu blokla


def anomali_kontrol(oturum_id: str, neden: str) -> tuple[bool, str]:
    """
    Oturum başına anormal davranışı izle.
    Döner: (devam_et, durum_mesaji)
    """
    if neden in ("injection",):
        _oturum_sayac[oturum_id] = _oturum_sayac.get(oturum_id, 0) + 2
    elif neden != "ok":
        _oturum_sayac[oturum_id] = _oturum_sayac.get(oturum_id, 0) + 1

    sayac = _oturum_sayac.get(oturum_id, 0)

    if sayac >= BLOK_ESIGI:
        logger.error(
            f"[ANOMALİ] Oturum bloklandı: {oturum_id} "
            f"| Sayaç: {sayac} | Son neden: {neden}"
        )
        return False, "blok"

    if sayac >= ANOMALI_ESIGI:
        logger.warning(
            f"[ANOMALİ] Şüpheli oturum: {oturum_id} "
            f"| Sayaç: {sayac} | Son neden: {neden}"
        )

    return True, "devam"


def konusma_logla(
    oturum_id: str,
    agent: str,
    kullanici_mesaji: str,
    input_sonuc: str,
    output_mudahale: bool,
    output_neden: str,
    yanit_uzunlugu: int,
):
    """Her konuşmayı yapılandırılmış log olarak yaz."""
    logger.info(json.dumps({
        "ts": datetime.utcnow().isoformat(),
        "oturum": oturum_id,
        "agent": agent,
        "mesaj_uzunlugu": len(kullanici_mesaji),
        "input_sonuc": input_sonuc,
        "output_mudahale": output_mudahale,
        "output_neden": output_neden,
        "yanit_uzunlugu": yanit_uzunlugu,
    }, ensure_ascii=False))


# ══════════════════════════════════════════════════
# FASTAPI MIDDLEWARE
# ══════════════════════════════════════════════════

class AgentGuardMiddleware(BaseHTTPMiddleware):
    """
    /api/v1/agent/ altındaki endpoint'lere otomatik uygular.
    main.py'e eklemek için:
        app.add_middleware(AgentGuardMiddleware)
    """

    KORUNAN_PREFIXLER = ["/api/v1/agents/callcenter", "/api/v1/agents/sizewin"]

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Sadece korunan endpoint'lerde çalış
        if not any(request.url.path.startswith(p) for p in self.KORUNAN_PREFIXLER):
            return await call_next(request)

        # Oturum ID al veya oluştur
        oturum_id = request.headers.get("X-Session-ID", str(uuid.uuid4()))
        agent = request.url.path.split("/")[-1]

        # ── Blok kontrolü
        devam, durum = anomali_kontrol(oturum_id, "check")
        if durum == "blok":
            return JSONResponse(
                status_code=429,
                content={"error": "Bu konuda yardımcı olamıyorum."}
            )

        # ── Request body oku
        body_bytes = await request.body()
        try:
            body = json.loads(body_bytes)
            kullanici_mesaji = body.get("message", body.get("question", ""))
        except Exception:
            kullanici_mesaji = ""

        # ── KATMAN 1: Input Guard
        gecerli, neden = input_guard(kullanici_mesaji)
        if not gecerli:
            anomali_kontrol(oturum_id, neden)
            konusma_logla(oturum_id, agent, kullanici_mesaji, neden, False, "ok", 0)
            return JSONResponse(
                status_code=200,
                content={"reply": YONLENDIRME.get(neden, YONLENDIRME["injection"])}
            )

        # ── Request'i downstream'e ilet
        response = await call_next(request)

        # ── KATMAN 4: Output Guard
        response_body = b""
        async for chunk in response.body_iterator:
            response_body += chunk

        try:
            response_data = json.loads(response_body)
            # Agent'lar "answer" döndürür; fallback "reply"
            yanit_alani = "answer" if "answer" in response_data else "reply"
            yanit_metni = response_data.get(yanit_alani, "")

            temiz_yanit, mudahale, output_neden = output_guard(yanit_metni)

            if mudahale:
                response_data[yanit_alani] = temiz_yanit
                response_body = json.dumps(response_data, ensure_ascii=False).encode()

        except Exception:
            output_neden = "parse_error"
            mudahale = False

        # ── KATMAN 5: Log
        konusma_logla(
            oturum_id, agent, kullanici_mesaji,
            "ok", mudahale, output_neden,
            len(response_body)
        )

        return Response(
            content=response_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type="application/json",
        )


# ══════════════════════════════════════════════════
# BAĞIMSIZ KULLANIM — MCP TOOL WRAPPER
# ══════════════════════════════════════════════════

def financial_tool_wrapper(tool_fn):
    """
    get_product_financial_datas'ı saran decorator.
    Yalnızca malzeme alanlarını döndürür.

    Kullanım:
        @financial_tool_wrapper
        async def get_product_financial_datas(stock_code: str):
            ...
    """
    import functools

    @functools.wraps(tool_fn)
    async def wrapper(*args, **kwargs):
        raw = await tool_fn(*args, **kwargs)
        if isinstance(raw, dict):
            return financial_field_filter(raw)
        if isinstance(raw, list):
            return [financial_field_filter(item) for item in raw]
        return raw

    return wrapper
