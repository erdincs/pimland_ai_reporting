"""Daily Brief — profil + zamanlama yönetimi ve brief üretim endpoint'leri."""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session, get_session

router = APIRouter(prefix="/daily-brief", tags=["Daily Brief"])

_TENANT = "upagon"

_FREQ_LABELS = {
    "daily":     "Günlük",
    "weekly":    "Haftalık",
    "monthly":   "Aylık",
    "adhoc":     "Manuel",
    "threshold": "Eşik Bazlı",
}

# ── helpers ──────────────────────────────────────────────────────────────────

def _j(v: Any) -> str:
    if v is None:
        return "null"
    return json.dumps(v, default=str)


def _t(v: Any):
    from datetime import time as dt_time
    if v is None:
        return None
    if isinstance(v, dt_time):
        return v
    h, m = str(v).split(":")[:2]
    return dt_time(int(h), int(m))


# ── Rol şablonları ─────────────────────────────────────────────────────────────

_TEMPLATES: Dict[str, Dict] = {
    "ceo": {
        "key":         "ceo",
        "label":       "CEO / Genel Müdür",
        "description": "Stratejik bakış, risk odaklı. Satış + kıyaslama ağırlıklı.",
        "tone":        "yonetici",
        "icon":        "🎩",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "06:00",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "yonetici",
                "length":        "ozet",
                "questions": [
                    {"question_text": "Dün toplam ciro hedefin neresinde? Hangi bölgeler hedefi aştı, hangileri altında kaldı?", "agent": "satis", "importance": "kritik"},
                    {"question_text": "Günlük satış trendi son 7 gün nasıl? Hafta başından bu yana kümülatif durum?", "agent": "satis", "importance": "kritik"},
                    {"question_text": "Hangi mağazalar sürekli hedef altında kalıyor? Müdahale gereken noktalar?", "agent": "satis", "importance": "yuksek"},
                    {"question_text": "E-ticarette dünkü performans — ciro, sipariş ve dönüşüm oranı nasıl?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "En çok satan ürün grupları hangileri? Stok kritik olan ürün var mı?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
            {
                "name":          "Haftalık Özet",
                "frequency_type": "weekly",
                "schedule_time": "07:00",
                "active_days":   [1],
                "tone":          "yonetici",
                "length":        "detay",
                "questions": [
                    {"question_text": "Bu hafta dikkat etmem gereken en önemli 3 risk nedir? Hangi konulara karar vermem lazım?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Sezon başından bu yana hedef gerçekleşme oranı nedir? Trende göre ay sonu tahmini?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "İade oranı yüksek ürünlerin enrichment kalite puanı düşük mü? Hangilerine müdahale etmeliyim?", "agent": "kiyaslama", "importance": "orta", "is_cross_domain": True},
                    {"question_text": "Mağaza verimliliği (MDO) trendi nasıl? Öne çıkan mağazalar?", "agent": "satis", "importance": "orta"},
                ],
            },
            {
                "name":          "Aylık Rapor",
                "frequency_type": "monthly",
                "schedule_time": "08:00",
                "active_days":   [1],
                "tone":          "analitik",
                "length":        "detay",
                "questions": [
                    {"question_text": "Bu ay hedef vs gerçekleşme oranı nedir? Hangi segment öne çıktı?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Aylık iade oranı ve trend analizi — hangi kategori öne çıkıyor?", "agent": "kiyaslama", "importance": "yuksek"},
                ],
            },
        ],
        "checklist": [
            {"text": "Bölge müdürleriyle haftalık görüşme", "trigger_rule": "çarşamba", "priority": "high"},
            {"text": "Sezon hedef değerlendirme toplantısı hazırlığı", "trigger_rule": "ay_sonu", "priority": "med"},
        ],
    },
    "satis_muduru": {
        "key":         "satis_muduru",
        "label":       "Satış Müdürü",
        "description": "Mağaza operasyonları, hedef takibi, MDO analizi.",
        "tone":        "operasyonel",
        "icon":        "🏪",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:30",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "operasyonel",
                "length":        "ozet",
                "questions": [
                    {"question_text": "Dünkü mağaza satışları — hedef vs gerçekleşme bölge bazında nasıl?", "agent": "satis", "importance": "kritik"},
                    {"question_text": "Hangi mağazalar hedefin altında kaldı? Ortak özellik veya neden var mı?", "agent": "satis", "importance": "kritik"},
                    {"question_text": "Günlük ortalama sepet tutarı ve MDO trendi son 7 gün nasıl?", "agent": "satis", "importance": "yuksek"},
                    {"question_text": "Stok kritik olan veya tükenmek üzere olan ürünler hangileri?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "İade oranı mağazalar arasında nasıl farklılaşıyor?", "agent": "satis", "importance": "orta"},
                ],
            },
            {
                "name":          "Haftalık Hedef Takibi",
                "frequency_type": "weekly",
                "schedule_time": "07:30",
                "active_days":   [1],
                "tone":          "operasyonel",
                "length":        "detay",
                "questions": [
                    {"question_text": "Haftalık hedef gerçekleşme oranı ve ay sonu projeksiyonu nedir?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "En iyi performans gösteren mağazaların ortak özelliği nedir?", "agent": "satis", "importance": "orta"},
                    {"question_text": "Kampanya dönemlerinde mağaza trafiği ve dönüşüm oranı değişimi?", "agent": "satis", "importance": "dusuk"},
                ],
            },
        ],
        "checklist": [
            {"text": "Mağaza müdürleriyle haftalık brifing", "trigger_rule": "pazartesi", "priority": "high"},
            {"text": "Ay sonu hedef kapanış raporunu gönder", "trigger_rule": "ay_sonu", "priority": "high"},
        ],
    },
    "eticaret_muduru": {
        "key":         "eticaret_muduru",
        "label":       "E-Ticaret Müdürü",
        "description": "Online satış, dönüşüm, kanal analizi, iade yönetimi.",
        "tone":        "analitik",
        "icon":        "🛒",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "08:00",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "analitik",
                "length":        "ozet",
                "questions": [
                    {"question_text": "Dünkü e-ticaret cirosu, sipariş adedi ve dönüşüm oranı nedir?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Kanal performansı nasıl? Web, mobil ve marketplace karşılaştırması?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Sepet terk oranı ve son 7 günlük trendi nasıl?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "En çok satan ve sepete eklenip satın alınmayan ürünler hangileri?", "agent": "eticaret", "importance": "yuksek"},
                ],
            },
            {
                "name":          "Haftalık Performans",
                "frequency_type": "weekly",
                "schedule_time": "09:00",
                "active_days":   [5],
                "tone":          "analitik",
                "length":        "detay",
                "questions": [
                    {"question_text": "Online iade oranı kategoriye göre nasıl farklılaşıyor?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Aktif kampanyaların dönüşüme etkisi nedir? Hangi kampanya öne çıkıyor?", "agent": "eticaret", "importance": "orta"},
                    {"question_text": "Enrichment kalitesi düşük ürünlerin online performansı nasıl etkileniyor?", "agent": "kiyaslama", "importance": "orta", "is_cross_domain": True},
                ],
            },
            {
                "name":          "Acil Durum",
                "frequency_type": "adhoc",
                "schedule_time": "09:00",
                "active_days":   [1, 2, 3, 4, 5, 6, 7],
                "tone":          "operasyonel",
                "length":        "kisa",
                "questions": [
                    {"question_text": "Kritik performans düşüşü: Bugün hangi kanal veya kategori anormal sapma gösteriyor?", "agent": "eticaret", "importance": "kritik"},
                ],
            },
        ],
        "checklist": [
            {"text": "Haftalık e-ticaret performans raporu hazırla", "trigger_rule": "cuma", "priority": "high"},
            {"text": "Kampanya optimizasyon toplantısı", "trigger_rule": "çarşamba", "priority": "med"},
        ],
    },
    "urun_yoneticisi": {
        "key":         "urun_yoneticisi",
        "label":       "Ürün Yöneticisi",
        "description": "PLM kalite, zenginleştirme, görsel ve içerik takibi.",
        "tone":        "operasyonel",
        "icon":        "📦",
        "schedules": [
            {
                "name":          "Günlük Kalite Takibi",
                "frequency_type": "daily",
                "schedule_time": "09:00",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "operasyonel",
                "length":        "ozet",
                "questions": [
                    {"question_text": "Enrichment kalitesi düşük (D/F grade) ürünler hangileri? Öncelikli aksiyon listesi?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Eksik görsel veya içerik bulunan ürünler ve öncelik sıralaması nasıl?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Bu sezon yeni eklenen ürünlerin kalite durumu nedir?", "agent": "urun_yonetimi", "importance": "yuksek"},
                ],
            },
            {
                "name":          "Haftalık Analiz",
                "frequency_type": "weekly",
                "schedule_time": "10:00",
                "active_days":   [1],
                "tone":          "analitik",
                "length":        "detay",
                "questions": [
                    {"question_text": "İade oranı yüksek ürünlerin PLM kalite puanıyla ilişkisi nedir?", "agent": "kiyaslama", "importance": "yuksek", "is_cross_domain": True},
                    {"question_text": "Hangi kategorilerde en fazla kalite sorunu görülüyor? Trend analizi?", "agent": "urun_yonetimi", "importance": "orta"},
                    {"question_text": "E-ticaret etiketi eksik ürünler listesi ve tamamlanma yüzdesi?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
        ],
        "checklist": [
            {"text": "Haftalık enrichment raporu takibi", "trigger_rule": "pazartesi", "priority": "high"},
            {"text": "Yeni sezon ürün kalite değerlendirmesi", "trigger_rule": "sezon_basi", "priority": "high"},
        ],
    },
    "bos": {
        "key":         "bos",
        "label":       "Boş Başlangıç",
        "description": "Soru olmadan başla, kendin özelleştir.",
        "tone":        "yonetici",
        "icon":        "📄",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:00",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "yonetici",
                "length":        "ozet",
                "questions": [],
            },
        ],
        "checklist": [],
    },
    # ── Eticaret SKL Profilleri ────────────────────────────────────────────────
    "eticaret_genel_mudur": {
        "key":         "eticaret_genel_mudur",
        "label":       "E-Ticaret Genel Müdür (SKL-01)",
        "description": "Günlük KPI özeti: net ciro, kanal nabzı, hedef tempo, uyarılar. 07:30'da gönderilir.",
        "tone":        "yonetici",
        "icon":        "🎯",
        "skl":         "skl01",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:30",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "yonetici",
                "length":        "ozet",
                "questions": [
                    {"question_text": "Dün e-ticaret toplam net ve brüt ciro ile iade/iptal makası nedir? Kanal bazında dağılımı ver.", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Tüm kanallarda dünün WoW trendi nasıl? Anormal düşüş veya artış gösteren kanal var mı?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Bu ay başından bugüne kadar günlük ortalama satış ne kadar? MoM karşılaştırması nedir?", "agent": "eticaret", "importance": "yuksek"},
                ],
            },
        ],
        "checklist": [
            {"text": "Kanal düşüşü varsa kanal yöneticisiyle görüş", "trigger_rule": "haftalik", "priority": "high"},
        ],
    },
    "eticaret_satis_ops": {
        "key":         "eticaret_satis_ops",
        "label":       "Satış & Operasyon Müdürü (SKL-02)",
        "description": "Günlük ürün performansı, iade/iptal analizi, katalog sağlığı. 07:00'de gönderilir.",
        "tone":        "operasyonel",
        "icon":        "📊",
        "skl":         "skl02",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:00",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "operasyonel",
                "length":        "detay",
                "questions": [
                    {"question_text": "Dün en çok satan 5 ürün (ciro ve adet bazında)? WoW değişimi nedir?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Dün iade oranı en yüksek ürünler hangileri? Beden bazında spike var mı?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "İptal oranı dünkü seviyesi ve son 7 günlük trendi nedir?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "7 günden uzun süredir satış yapmayan aktif ürünler kaç tane? Hangi kategorilerde yoğun?", "agent": "eticaret", "importance": "yuksek"},
                ],
            },
        ],
        "checklist": [
            {"text": "İade spike'ı varsa ürün ekibiyle görüş", "trigger_rule": "haftalik", "priority": "high"},
        ],
    },
    "eticaret_urun_katalog": {
        "key":         "eticaret_urun_katalog",
        "label":       "Ürün & Katalog Müdürü (SKL-03)",
        "description": "Enrichment grade dağılımı, sıralama sağlığı, sezon durumu. 08:00'de gönderilir.",
        "tone":        "operasyonel",
        "icon":        "📦",
        "skl":         "skl03",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "08:00",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "operasyonel",
                "length":        "detay",
                "questions": [
                    {"question_text": "Güncel enrichment grade dağılımı nedir? A/B/C/D/F yüzdeleri ve dün kaç ürün A'ya yükseldi?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Aktif ürün sayısı kaç? Görselsiz ya da bloke olan aktif ürün var mı?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "14 gün veya daha fazla süredir sıralama güncellenmemiş kategori var mı?", "agent": "urun_yonetimi", "importance": "yuksek"},
                ],
            },
        ],
        "checklist": [
            {"text": "Haftalık enrichment raporu takibi", "trigger_rule": "pazartesi", "priority": "high"},
        ],
    },
    "eticaret_pazarlama": {
        "key":         "eticaret_pazarlama",
        "label":       "Pazarlama Müdürü (SKL-04)",
        "description": "Kanal performansı, trafik kalitesi, kampanya durumu. 07:30'da gönderilir.",
        "tone":        "analitik",
        "icon":        "📣",
        "skl":         "skl04",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:30",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "analitik",
                "length":        "ozet",
                "questions": [
                    {"question_text": "Dün kanal bazında net ciro dağılımı ve WoW delta? Öne çıkan veya düşüş gösteren kanal?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Dün en iyi ve en kötü performanslı 3 kanal ve nedenleri neler?", "agent": "eticaret", "importance": "yuksek"},
                ],
            },
        ],
        "checklist": [
            {"text": "Haftalık e-ticaret performans raporu hazırla", "trigger_rule": "cuma", "priority": "high"},
        ],
    },
    "eticaret_teknoloji": {
        "key":         "eticaret_teknoloji",
        "label":       "Teknoloji & Süreç Müdürü (SKL-05)",
        "description": "Sistem sağlığı, pipeline durumu, ekip üretkenliği. 06:30'da gönderilir.",
        "tone":        "operasyonel",
        "icon":        "⚙️",
        "skl":         "skl05",
        "schedules": [
            {
                "name":          "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "06:30",
                "active_days":   [1, 2, 3, 4, 5],
                "tone":          "operasyonel",
                "length":        "ozet",
                "questions": [
                    {"question_text": "Son 24 saatte tüm sync jobları başarılı mı? Hata veya gecikme var mı?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Enrichment durumu: dün kaç ürün işlendi? D/F grade bekleyen ürün sayısı nedir?", "agent": "urun_yonetimi", "importance": "yuksek"},
                ],
            },
        ],
        "checklist": [
            {"text": "Haftalık teknik sorun değerlendirmesi", "trigger_rule": "pazartesi", "priority": "med"},
        ],
    },
}


# ── ŞABLONLAR ─────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates() -> dict:
    """Rol bazlı profil şablonları — zamanlama + soru önizlemesi."""
    return {
        "templates": [
            {
                "key":         t["key"],
                "label":       t["label"],
                "description": t["description"],
                "tone":        t["tone"],
                "icon":        t["icon"],
                "schedule_count": len(t["schedules"]),
                "schedules_preview": [
                    {
                        "name":           s["name"],
                        "frequency_type": s["frequency_type"],
                        "frequency_label": _FREQ_LABELS.get(s["frequency_type"], s["frequency_type"]),
                        "schedule_time":  s["schedule_time"],
                        "question_count": len(s["questions"]),
                    }
                    for s in t["schedules"]
                ],
            }
            for t in _TEMPLATES.values()
        ]
    }


# ── PROFIL CRUD ───────────────────────────────────────────────────────────────

@router.get("/profiles")
async def list_profiles(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    only_active: bool = Query(default=False),
) -> dict:
    where = "WHERE p.tenant_id = :tenant"
    params: Dict[str, Any] = {"tenant": _TENANT}
    if only_active:
        where += " AND p.is_active = true"

    rows = (await session.execute(text(f"""
        SELECT
            p.id, p.profile_id, p.name, p.role, p.owner_email,
            p.timezone, p.is_active, p.created_at, p.updated_at,
            (SELECT COUNT(*) FROM brief_schedules s
             WHERE s.profile_id = p.id AND s.is_active = true) AS schedule_count,
            (SELECT COUNT(*) FROM brief_checklist_items c
             WHERE c.profile_id = p.id AND c.is_active = true) AS checklist_count,
            (SELECT MAX(h.generated_at) FROM brief_history h
             WHERE h.profile_id = p.id) AS last_brief_at
        FROM brief_profiles p
        {where}
        ORDER BY p.created_at
    """), params)).mappings().all()

    return {"profiles": [dict(r) for r in rows]}


@router.get("/profiles/{profile_id}")
async def get_profile(
    profile_id: int,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    profile = (await session.execute(
        text("SELECT * FROM brief_profiles WHERE id = :pid"),
        {"pid": profile_id},
    )).mappings().first()
    if not profile:
        raise HTTPException(404, "Profil bulunamadı")

    schedules = (await session.execute(text("""
        SELECT s.*,
               (SELECT COUNT(*) FROM brief_questions q
                WHERE q.schedule_id = s.id AND q.is_active = true) AS question_count
        FROM brief_schedules s
        WHERE s.profile_id = :pid
        ORDER BY s.created_at
    """), {"pid": profile_id})).mappings().all()

    checklist = (await session.execute(text("""
        SELECT * FROM brief_checklist_items
        WHERE profile_id = :pid AND is_active = true
        ORDER BY sort_order, id
    """), {"pid": profile_id})).mappings().all()

    sched_list = []
    for s in schedules:
        d = dict(s)
        d["frequency_label"] = _FREQ_LABELS.get(d.get("frequency_type", "daily"), "Günlük")
        sched_list.append(d)

    return {
        "profile":   dict(profile),
        "schedules": sched_list,
        "checklist": [dict(c) for c in checklist],
    }


@router.post("/profiles")
async def create_profile(
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Profil oluştur. template_key verilirse şablon zamanlamaları + sorular seed edilir."""
    pid = payload.get("profile_id") or f"profile_{int(time.time())}"
    result = await session.execute(text("""
        INSERT INTO brief_profiles
            (profile_id, name, role, owner_email, timezone, tenant_id)
        VALUES
            (:profile_id, :name, :role, :owner_email, :timezone, :tenant)
        RETURNING id
    """), {
        "profile_id":  pid,
        "name":        payload["name"],
        "role":        payload.get("role"),
        "owner_email": payload.get("owner_email"),
        "timezone":    payload.get("timezone", "Europe/Istanbul"),
        "tenant":      payload.get("tenant_id", _TENANT),
    })
    new_id = result.scalar()

    template_key = payload.get("template_key", "bos")
    tmpl = _TEMPLATES.get(template_key, _TEMPLATES["bos"])

    for sched in tmpl["schedules"]:
        s_result = await session.execute(text("""
            INSERT INTO brief_schedules
                (profile_id, name, frequency_type, schedule_time,
                 active_days, send_email, tone, length, format, top_insight_count)
            VALUES
                (:pid, :name, :freq, :stime,
                 CAST(:days AS JSONB), :email, :tone, :length, :fmt, :topn)
            RETURNING id
        """), {
            "pid":    new_id,
            "name":   sched["name"],
            "freq":   sched["frequency_type"],
            "stime":  _t(sched.get("schedule_time", "07:00")),
            "days":   _j(sched.get("active_days", [1, 2, 3, 4, 5])),
            "email":  sched.get("send_email", True),
            "tone":   sched.get("tone", tmpl["tone"]),
            "length": sched.get("length", "ozet"),
            "fmt":    sched.get("format", "mixed"),
            "topn":   sched.get("top_insight_count", 3),
        })
        sched_id = s_result.scalar()

        for i, q in enumerate(sched.get("questions", [])):
            await session.execute(text("""
                INSERT INTO brief_questions
                    (schedule_id, question_text, agent, importance,
                     is_cross_domain, trigger_days, sort_order)
                VALUES (:sid, :qtxt, :agent, :imp, :cross,
                        CAST(:tdays AS JSONB), :sord)
            """), {
                "sid":   sched_id,
                "qtxt":  q["question_text"],
                "agent": q["agent"],
                "imp":   q.get("importance", "orta"),
                "cross": q.get("is_cross_domain", False),
                "tdays": _j(q.get("trigger_days")),
                "sord":  i,
            })

    for i, item in enumerate(tmpl.get("checklist", [])):
        await session.execute(text("""
            INSERT INTO brief_checklist_items
                (profile_id, text, priority, trigger_rule, sort_order)
            VALUES (:pid, :txt, :pri, :rule, :sord)
        """), {
            "pid":  new_id,
            "txt":  item["text"],
            "pri":  item.get("priority", "med"),
            "rule": item.get("trigger_rule"),
            "sord": i,
        })

    await session.commit()
    return {"id": new_id, "message": "Profil oluşturuldu", "template": template_key}


@router.put("/profiles/{profile_id}")
async def update_profile(
    profile_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _ALLOWED = {"name", "role", "owner_email", "timezone", "is_active"}
    updates = {k: v for k, v in payload.items() if k in _ALLOWED}
    if not updates:
        return {"message": "Güncelleme yok"}
    parts = [f"{k} = :{k}" for k in updates]
    params = {**updates, "pid": profile_id}
    await session.execute(
        text(f"UPDATE brief_profiles SET {', '.join(parts)}, updated_at = NOW() WHERE id = :pid"),
        params,
    )
    await session.commit()
    return {"message": "Profil güncellendi"}


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_profiles WHERE id = :pid"),
        {"pid": profile_id},
    )
    await session.commit()
    return {"message": "Profil silindi"}


# ── ZAMANLAMA CRUD ────────────────────────────────────────────────────────────

@router.get("/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: int,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    s = (await session.execute(
        text("SELECT * FROM brief_schedules WHERE id = :sid"),
        {"sid": schedule_id},
    )).mappings().first()
    if not s:
        raise HTTPException(404, "Zamanlama bulunamadı")

    questions = (await session.execute(text("""
        SELECT * FROM brief_questions
        WHERE schedule_id = :sid AND is_active = true
        ORDER BY sort_order, id
    """), {"sid": schedule_id})).mappings().all()

    d = dict(s)
    d["frequency_label"] = _FREQ_LABELS.get(d.get("frequency_type", "daily"), "Günlük")
    return {
        "schedule":  d,
        "questions": [dict(q) for q in questions],
    }


@router.post("/profiles/{profile_id}/schedules")
async def create_schedule(
    profile_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    result = await session.execute(text("""
        INSERT INTO brief_schedules
            (profile_id, name, frequency_type, schedule_time,
             active_days, send_email, tone, length, format, top_insight_count)
        VALUES
            (:pid, :name, :freq, :stime,
             CAST(:days AS JSONB), :email, :tone, :length, :fmt, :topn)
        RETURNING id
    """), {
        "pid":    profile_id,
        "name":   payload.get("name", _FREQ_LABELS.get(payload.get("frequency_type", "daily"), "Brief")),
        "freq":   payload.get("frequency_type", "daily"),
        "stime":  _t(payload.get("schedule_time", "07:00")),
        "days":   _j(payload.get("active_days", [1, 2, 3, 4, 5])),
        "email":  payload.get("send_email", True),
        "tone":   payload.get("tone", "yonetici"),
        "length": payload.get("length", "ozet"),
        "fmt":    payload.get("format", "mixed"),
        "topn":   payload.get("top_insight_count", 3),
    })
    new_id = result.scalar()
    await session.commit()
    return {"id": new_id, "message": "Zamanlama oluşturuldu"}


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _ALLOWED = {
        "name", "frequency_type", "schedule_time", "active_days",
        "is_active", "send_email", "tone", "length", "format", "top_insight_count",
    }
    updates = {k: v for k, v in payload.items() if k in _ALLOWED}
    if not updates:
        return {"message": "Güncelleme yok"}

    parts, params = [], {"sid": schedule_id}
    for key, value in updates.items():
        if key == "active_days":
            parts.append("active_days = CAST(:active_days AS JSONB)")
            params["active_days"] = _j(value)
        elif key == "schedule_time":
            parts.append("schedule_time = :schedule_time")
            params["schedule_time"] = _t(value)
        elif key == "format":
            parts.append("format = :fmt")
            params["fmt"] = value
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value

    await session.execute(
        text(f"UPDATE brief_schedules SET {', '.join(parts)}, updated_at = NOW() WHERE id = :sid"),
        params,
    )
    await session.commit()
    return {"message": "Zamanlama güncellendi"}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_schedules WHERE id = :sid"),
        {"sid": schedule_id},
    )
    await session.commit()
    return {"message": "Zamanlama silindi"}


# ── SORU YÖNETİMİ ─────────────────────────────────────────────────────────────

@router.get("/library/questions")
async def list_question_library(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    category: Optional[str] = None,
    department: Optional[str] = None,
    data_status: Optional[str] = None,
    frequency: Optional[str] = None,
    importance: Optional[str] = None,
    q: Optional[str] = None,
) -> dict:
    where = ["is_active = true"]
    params: Dict[str, Any] = {}
    if category:
        where.append("category_code = :cat"); params["cat"] = category
    if department:
        where.append("department = :dept"); params["dept"] = department
    if data_status:
        where.append("data_status = :ds"); params["ds"] = data_status
    if frequency:
        where.append("frequency = :freq"); params["freq"] = frequency
    if importance:
        where.append("importance = :imp"); params["imp"] = importance
    if q:
        where.append("question_text ILIKE :q"); params["q"] = f"%{q}%"

    rows = (await session.execute(text(f"""
        SELECT * FROM brief_question_library
        WHERE {' AND '.join(where)}
        ORDER BY category_code, sort_order, id
    """), params)).mappings().all()

    grouped: Dict[str, List] = {}
    for r in rows:
        grouped.setdefault(r["category"] or r["category_code"] or "Diğer", []).append(dict(r))
    return {"library": grouped, "total": len(rows)}


@router.get("/library/stats")
async def library_stats(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    rows = (await session.execute(text("""
        SELECT
            department,
            COUNT(*) FILTER (WHERE is_active) AS total,
            COUNT(*) FILTER (WHERE data_status = 'available' AND is_active) AS available,
            COUNT(*) FILTER (WHERE data_status = 'partial' AND is_active) AS partial,
            COUNT(*) FILTER (WHERE data_status = 'integration_needed' AND is_active) AS integration_needed,
            COUNT(*) FILTER (WHERE frequency = 'daily' AND is_active) AS daily,
            COUNT(*) FILTER (WHERE frequency = 'weekly' AND is_active) AS weekly,
            COUNT(*) FILTER (WHERE frequency = 'monthly' AND is_active) AS monthly
        FROM brief_question_library
        GROUP BY department
    """))).mappings().all()

    cat_rows = (await session.execute(text("""
        SELECT category_code, category, COUNT(*) AS cnt,
               COUNT(*) FILTER (WHERE data_status = 'available') AS available,
               COUNT(*) FILTER (WHERE data_status = 'partial') AS partial,
               COUNT(*) FILTER (WHERE data_status = 'integration_needed') AS integration_needed
        FROM brief_question_library
        WHERE is_active = true
        GROUP BY category_code, category
        ORDER BY category_code
    """))).mappings().all()

    return {
        "by_department": [dict(r) for r in rows],
        "by_category": [dict(r) for r in cat_rows],
    }


@router.post("/library/questions")
async def create_library_question(
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    result = await session.execute(text("""
        INSERT INTO brief_question_library
          (question_code, category_code, category, department, agent, agent_label,
           question_text, importance, frequency, data_status, data_sources,
           constraints_note, is_cross_domain, sort_order)
        VALUES
          (:code, :cat_code, :category, :dept, :agent, :agent_label,
           :text, :imp, :freq, :status, CAST(:sources AS JSONB),
           :constraint, :cross, :sort)
        RETURNING id
    """), {
        "code":        payload.get("question_code"),
        "cat_code":    payload.get("category_code"),
        "category":    payload.get("category", ""),
        "dept":        payload.get("department", "eticaret"),
        "agent":       payload.get("agent", "eticaret"),
        "agent_label": payload.get("agent_label"),
        "text":        payload["question_text"],
        "imp":         payload.get("importance", "orta"),
        "freq":        payload.get("frequency", "daily"),
        "status":      payload.get("data_status", "available"),
        "sources":     _j(payload.get("data_sources", [])),
        "constraint":  payload.get("constraints_note"),
        "cross":       payload.get("is_cross_domain", False),
        "sort":        payload.get("sort_order", 0),
    })
    new_id = result.scalar()
    await session.commit()
    return {"id": new_id, "message": "Soru eklendi"}


@router.put("/library/questions/{lib_id}")
async def update_library_question(
    lib_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _ALLOWED = {
        "question_text", "importance", "frequency", "data_status",
        "data_sources", "constraints_note", "agent", "agent_label",
        "category", "category_code", "department", "is_cross_domain",
        "sort_order", "is_active", "question_code",
    }
    updates = {k: v for k, v in payload.items() if k in _ALLOWED}
    if not updates:
        return {"message": "Güncelleme yok"}
    parts, params = [], {"lid": lib_id}
    for key, value in updates.items():
        if key == "data_sources":
            parts.append("data_sources = CAST(:data_sources AS JSONB)")
            params["data_sources"] = _j(value)
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value
    await session.execute(
        text(f"UPDATE brief_question_library SET {', '.join(parts)} WHERE id = :lid"),
        params,
    )
    await session.commit()
    return {"message": "Soru güncellendi"}


@router.delete("/library/questions/{lib_id}")
async def delete_library_question(
    lib_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_question_library WHERE id = :lid"),
        {"lid": lib_id},
    )
    await session.commit()
    return {"message": "Soru silindi"}


@router.post("/schedules/{schedule_id}/questions")
async def add_question(
    schedule_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    next_order = (await session.execute(text("""
        SELECT COALESCE(MAX(sort_order), 0) + 1
        FROM brief_questions WHERE schedule_id = :sid
    """), {"sid": schedule_id})).scalar() or 1

    result = await session.execute(text("""
        INSERT INTO brief_questions
            (schedule_id, question_text, agent, importance,
             is_cross_domain, trigger_days, sort_order)
        VALUES
            (:sid, :qtxt, :agent, :importance,
             :is_cross, CAST(:tdays AS JSONB), :sord)
        RETURNING id
    """), {
        "sid":       schedule_id,
        "qtxt":      payload["question_text"],
        "agent":     payload["agent"],
        "importance": payload.get("importance", "orta"),
        "is_cross":  payload.get("is_cross_domain", False),
        "tdays":     _j(payload.get("trigger_days")),
        "sord":      next_order,
    })
    new_id = result.scalar()

    if payload.get("library_id"):
        await session.execute(text("""
            UPDATE brief_question_library
            SET usage_count = usage_count + 1 WHERE id = :lid
        """), {"lid": payload["library_id"]})

    await session.commit()
    return {"id": new_id}


@router.put("/questions/{question_id}")
async def update_question(
    question_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _ALLOWED = {"question_text", "agent", "importance", "is_cross_domain",
                "trigger_days", "sort_order", "is_active"}
    updates = {k: v for k, v in payload.items() if k in _ALLOWED}
    if not updates:
        return {"message": "Güncelleme yok"}

    parts, params = [], {"qid": question_id}
    for key, value in updates.items():
        if key == "trigger_days":
            parts.append("trigger_days = CAST(:tdays AS JSONB)")
            params["tdays"] = _j(value)
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value

    await session.execute(
        text(f"UPDATE brief_questions SET {', '.join(parts)} WHERE id = :qid"),
        params,
    )
    await session.commit()
    return {"message": "Soru güncellendi"}


@router.delete("/questions/{question_id}")
async def delete_question(
    question_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_questions WHERE id = :qid"),
        {"qid": question_id},
    )
    await session.commit()
    return {"message": "Soru silindi"}


@router.post("/questions/reorder")
async def reorder_questions(
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    for order, qid in enumerate(payload.get("ordered_ids", [])):
        await session.execute(
            text("UPDATE brief_questions SET sort_order = :sord WHERE id = :qid"),
            {"sord": order, "qid": qid},
        )
    await session.commit()
    return {"message": "Sıralama kaydedildi"}


# ── KONTROL LİSTESİ ───────────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/checklist")
async def add_checklist_item(
    profile_id: int,
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    next_order = (await session.execute(text("""
        SELECT COALESCE(MAX(sort_order), 0) + 1
        FROM brief_checklist_items WHERE profile_id = :pid
    """), {"pid": profile_id})).scalar() or 1

    result = await session.execute(text("""
        INSERT INTO brief_checklist_items
            (profile_id, text, priority, trigger_rule, sort_order)
        VALUES (:pid, :txt, :priority, :rule, :sord)
        RETURNING id
    """), {
        "pid":      profile_id,
        "txt":      payload["text"],
        "priority": payload.get("priority", "med"),
        "rule":     payload.get("trigger_rule"),
        "sord":     next_order,
    })
    new_id = result.scalar()
    await session.commit()
    return {"id": new_id}


@router.delete("/checklist/{item_id}")
async def delete_checklist_item(
    item_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await session.execute(
        text("DELETE FROM brief_checklist_items WHERE id = :iid"),
        {"iid": item_id},
    )
    await session.commit()
    return {"message": "Madde silindi"}


@router.post("/checklist/toggle")
async def toggle_checklist_item(
    payload: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    today = date.today()
    is_done = bool(payload.get("is_done", False))
    done_at = datetime.utcnow() if is_done else None
    await session.execute(text("""
        INSERT INTO brief_checklist_state
            (profile_id, item_id, check_date, is_done, done_at)
        VALUES (:pid, :iid, :chk, :is_done, :done_at)
        ON CONFLICT (profile_id, item_id, check_date) DO UPDATE SET
            is_done = EXCLUDED.is_done, done_at = EXCLUDED.done_at
    """), {
        "pid":     payload["profile_id"],
        "iid":     payload["item_id"],
        "chk":     today,
        "is_done": is_done,
        "done_at": done_at,
    })
    await session.commit()
    return {"ok": True}


# ── BRIEF ÜRETME / OKUMA ──────────────────────────────────────────────────────

@router.post("/schedules/{schedule_id}/generate")
async def generate_brief_for_schedule(
    schedule_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    from app.services.daily_brief.orchestrator import generate_brief
    result = await generate_brief(schedule_id, session)
    if "hata" in result:
        raise HTTPException(400, result["hata"])
    return result


@router.post("/schedules/{schedule_id}/draft-preview")
async def draft_preview_for_schedule(
    schedule_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Gerçek veri çekmeden, sadece soru listesine dayalı taslak brief HTML üretir."""
    from app.services.daily_brief.composer import _converse

    sched = (await session.execute(text("""
        SELECT s.*, p.name AS prof_name, p.role, p.tone AS prof_tone
        FROM brief_schedules s
        JOIN brief_profiles p ON p.id = s.profile_id
        WHERE s.id = :sid
    """), {"sid": schedule_id})).mappings().first()
    if not sched:
        raise HTTPException(404, "Zamanlama bulunamadı")

    questions = (await session.execute(text("""
        SELECT * FROM brief_questions WHERE schedule_id = :sid
        ORDER BY sort_order, id
    """), {"sid": schedule_id})).mappings().all()

    if not questions:
        raise HTTPException(400, "Bu zamanlamada soru yok — önce soru ekleyin")

    tone = sched.get("tone") or sched.get("prof_tone") or "yonetici"
    tone_desc = {"yonetici": "stratejik, kısa, karar odaklı",
                 "operasyonel": "detaylı, operasyonel, somut sayılar",
                 "analitik": "analitik, trend odaklı, derinlemesine"}.get(tone, "stratejik")

    q_lines = "\n".join(
        f"{i+1}. [{q['agent'].upper()}] {q['question_text']}"
        for i, q in enumerate(questions)
    )

    system = """Sen Pimland e-ticaret yönetim briefi tasarımcısısın.
Verilen soru listesine göre bir yönetici brief TASLAK'ı oluştur.
Gerçek veri yok — temsili/kurgusal örnek sayılar kullan, her değerin yanına (taslak) ibaresi ekle.

ÇIKTI KURALLARI:
- SADECE HTML döndür. DOCTYPE/html/head/body TAG YOK. Doğrudan <style> bloğuyla başla.
- Renk paleti hex olarak kullan (CSS değişken yok): bg2=#13131a bg3=#1a1a24 bg4=#22222f border=#2a2a3a t1=#f0f0f8 t2=#9898b8 t3=#55556a orange=#ff6b2b teal=#00c2a8 green=#22c55e red=#ef4444 yellow=#f59e0b
- Font: Inter, system-ui, sans-serif. Font-size: 13px.
- .pw-wrap max-width:640px; margin:0 auto

BÖLÜM SIRASI (değiştirme):
1. Profil başlığı — küçük, renksiz, tarih placeholder
2. KPI strip — 4 kart (profil için anlamlı metrikler, kurgusal sayılar, "(taslak)" etiketi)
3. Soru kartları — agent grubuna göre grupla, her soru için 2-3 cümle örnek yanıt yaz
4. Uyarı bantları — 1-2 adet, uygun uyarı öner
5. BUGÜN ÖNCELİK — 3-4 aksiyon maddesi

Türkçe yaz. Bölüm başlıkları BÜYÜK HARF."""

    user = f"""Profil: {sched['prof_name']} — {sched.get('role') or 'Yönetici'}
Ton: {tone_desc}
Zamanlama: {sched.get('name', 'Günlük Brief')}

Sorular ({len(questions)} adet):
{q_lines}

Bu sorulara ve profile göre HTML brief taslağı oluştur."""

    html = await _converse(system, user, max_tokens=4000, temperature=0.6)

    # Strip code fences if Claude wraps in them
    if "```html" in html:
        html = html.split("```html", 1)[1].split("```", 1)[0].strip()
    elif "```" in html:
        html = html.split("```", 1)[1].split("```", 1)[0].strip()

    return {"html": html, "question_count": len(questions)}


@router.get("/schedules/{schedule_id}/brief/{brief_date}")
async def get_brief_for_schedule(
    schedule_id: int,
    brief_date: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    row = (await session.execute(text("""
        SELECT * FROM brief_history
        WHERE schedule_id = :sid AND brief_date = :bdate
    """), {"sid": schedule_id, "bdate": date.fromisoformat(brief_date)})).mappings().first()
    if not row:
        raise HTTPException(404, "Brief bulunamadı — önce üret")
    return dict(row)


@router.get("/briefs/{profile_id}/{brief_date}")
async def get_brief_for_profile(
    profile_id: int,
    brief_date: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    """Profil + tarih için en güncel daily brief'i döndürür (tüm zamanlamalar birleştirilir)."""
    rows = (await session.execute(text("""
        SELECT h.*, s.name AS schedule_name, s.frequency_type, s.tone
        FROM brief_history h
        JOIN brief_schedules s ON s.id = h.schedule_id
        WHERE h.profile_id = :pid
          AND h.brief_date = :bdate
          AND s.frequency_type = 'daily'
        ORDER BY h.generation_ms DESC
        LIMIT 1
    """), {"pid": profile_id, "bdate": date.fromisoformat(brief_date)})).mappings().all()

    if not rows:
        raise HTTPException(404, "Brief bulunamadı — önce üret")

    row = dict(rows[0])
    import json as _json
    for field in ("top_insights", "kpi_data", "qa_results", "checklist_state",
                  "actions", "agent_metadata", "executive_summary"):
        v = row.get(field)
        if isinstance(v, str):
            try:
                row[field] = _json.loads(v)
            except Exception:
                pass
    return row


@router.post("/briefs/{profile_id}/generate")
async def generate_brief_for_profile(
    profile_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Profilin tüm aktif günlük zamanlamaları için brief üretir ve birleşik sonuç döndürür."""
    from app.services.daily_brief.orchestrator import generate_brief as _gen

    rows = (await session.execute(text("""
        SELECT s.id, s.name
        FROM brief_schedules s
        WHERE s.profile_id = :pid
          AND s.is_active = true
          AND s.frequency_type = 'daily'
          AND (SELECT COUNT(*) FROM brief_questions q
               WHERE q.schedule_id = s.id AND q.is_active = true) > 0
        ORDER BY s.id
    """), {"pid": profile_id})).mappings().all()

    if not rows:
        raise HTTPException(404, "Bu profil için aktif günlük zamanlama bulunamadı")

    last_result = None
    for sched in rows:
        result = await _gen(sched["id"], session)
        if "hata" not in result:
            last_result = result

    if not last_result:
        raise HTTPException(500, "Brief üretilemedi")

    return last_result


@router.get("/profiles/{profile_id}/history")
async def get_profile_history(
    profile_id: int,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    limit: int = Query(default=30, le=100),
) -> dict:
    rows = (await session.execute(text("""
        SELECT h.*, s.name AS schedule_name, s.frequency_type
        FROM brief_history h
        LEFT JOIN brief_schedules s ON s.id = h.schedule_id
        WHERE h.profile_id = :pid
        ORDER BY h.brief_date DESC, h.generated_at DESC
        LIMIT :lim
    """), {"pid": profile_id, "lim": limit})).mappings().all()
    return {"history": [dict(r) for r in rows]}
