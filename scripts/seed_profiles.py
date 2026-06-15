"""Şablon profillerini DB'ye seed eder (idempotent — var olanları günceller)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import time as dt_time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncpg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

# ── Maksimum soru setleri (sadece data_status=available olan sorular) ──────────

TEMPLATES = {
    "ceo": {
        "name": "CEO / Genel Müdür",
        "role": "ceo",
        "tone": "yonetici",
        "top_insight_count": 5,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "06:00",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "yonetici",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    # SATIS — mağaza ağı
                    {"question_text": "Güncel ay için toplam mağaza satışı hedef gerçekleşme oranı nedir? Bölge bazında en iyi ve en kötü performans nerede?", "agent": "satis", "importance": "kritik"},
                    {"question_text": "MDO ve OBF trendi son 3 ay nasıl gidiyor? Sektör normuna göre neredeyiz?", "agent": "satis", "importance": "yuksek"},
                    # ETİCARET — online kanallar
                    {"question_text": "Geçen ayın toplam e-ticaret cirası ve yıllık hedefin neresindeyiz? Sezon başından bu yana trend yönü ne?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Brüt ciro ile net ciro arasındaki iade/iptal makası geçen aya göre nasıl değişti? Risk seviyesi nedir?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Kanal bazında ciro payı ve iade oranı dağılımı nasıl? Hangi kanal en yüksek marjı veriyor?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "En çok satan 10 ürün ve geçen haftaya göre büyüme/düşüş trendi?", "agent": "eticaret", "importance": "yuksek"},
                    # ÜRÜN YÖNETİMİ
                    {"question_text": "Stok kritik seviyeye düşen A ve B grade ürünler hangileri? Satış kaybı riski olan SKU sayısı?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Aktif ürünlerden son 7 günde hiç satışı olmayan kaç ürün var? Bu ürünlerin ortak özelliği nedir?", "agent": "urun_yonetimi", "importance": "orta"},
                    # KIYASLAMA — YTD/stratejik
                    {"question_text": "Mevcut trend devam ederse bu ay kapanışta hedeften sapma ne kadar olur? En riskli 2 senaryo nedir?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Sezon sonuna kadar stok tükenme projeksiyonu: Hangi kategoride erken stok tükenme riski var?", "agent": "kiyaslama", "importance": "yuksek"},
                ],
            },
            {
                "name": "Haftalık Özet",
                "frequency_type": "weekly",
                "schedule_time": "07:00",
                "active_days": [1],
                "tone": "yonetici",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    {"question_text": "Bu hafta ekip üyesi bazında üretkenlik raporu: enrichment ve kategori sıralama hedefleri tutturuldu mu?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Bu haftanın 3 en önemli önceliği nedir? Geçen haftadan devam eden kritik konu var mı?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Geçen yılın aynı ayıyla karşılaştırıldığında ciro, sipariş adedi ve sepet değeri nasıl değişti?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Haftalık en iyi performans gösteren mağazaların ortak özelliği nedir?", "agent": "satis", "importance": "orta"},
                ],
            },
        ],
    },

    "satis_muduru": {
        "name": "Satış Müdürü",
        "role": "satis_muduru",
        "tone": "operasyonel",
        "top_insight_count": 5,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:30",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "operasyonel",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    {"question_text": "Güncel ay toplam mağaza satışı — hedef vs gerçekleşme bölge bazında nasıl? Hangi bölge kritik?", "agent": "satis", "importance": "kritik"},
                    {"question_text": "Hedefin altında kalan mağazalar hangileri? Son 3 ay sürekli altında kalanlar var mı?", "agent": "satis", "importance": "kritik"},
                    {"question_text": "MDO ortalaması ve bölge bazında dağılımı nasıl? Sektör normunun altında kalan mağazalar?", "agent": "satis", "importance": "yuksek"},
                    {"question_text": "OBF (ortalama ürün başına fiyat) trendi aylık nasıl gidiyor? Sepet büyüklüğü değişimi?", "agent": "satis", "importance": "yuksek"},
                    {"question_text": "Dönemsel performans: Bu yılın ilk 5 ayı geçen yılın aynı dönemiyle kıyaslandığında ne değişti?", "agent": "satis", "importance": "orta"},
                    # Ürün desteği
                    {"question_text": "Stok kritik seviyeye düşen A ve B grade ürünler hangileri? Hangi mağazaları etkiliyor?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Aktif ürünlerden son 7 günde hiç satışı olmayan ürünler ve satış kaybı tahmini?", "agent": "urun_yonetimi", "importance": "orta"},
                    # Projeksiyon
                    {"question_text": "Mevcut trend devam ederse bu ay kapanışta hedeften sapma ne kadar olur?", "agent": "kiyaslama", "importance": "kritik"},
                ],
            },
            {
                "name": "Haftalık Hedef Takibi",
                "frequency_type": "weekly",
                "schedule_time": "07:30",
                "active_days": [1],
                "tone": "operasyonel",
                "length": "detay",
                "top_insight_count": 4,
                "questions": [
                    {"question_text": "Bu hafta ekip üyesi bazında üretkenlik ve kategori sıralama güncelleme sayısı?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Geçen yılın aynı dönemiyle mağaza ciro ve hedef gerçekleşme karşılaştırması?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Bu haftanın en iyi 5 ve en kötü 5 mağazası ve fark nedenleri?", "agent": "satis", "importance": "yuksek"},
                ],
            },
        ],
    },

    "eticaret_muduru": {
        "name": "E-Ticaret Müdürü",
        "role": "eticaret_muduru",
        "tone": "analitik",
        "top_insight_count": 5,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "08:00",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "analitik",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    {"question_text": "Geçen ayın toplam e-ticaret cirası ve yıllık hedefin neresindeyiz? Sezon başından bu yana kümülatif durum?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Bu hafta toplam ciro hedefin neresindeyiz? Hangi kanal hedefi aştı, hangisi altında kaldı?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Brüt ciro ile net ciro arasındaki makas ne kadar? İade ve iptal oranları geçen haftaya göre nasıl değişti?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "En çok satan 10 ürün hangileri? Ciro ve adet bazında sıralama, geçen haftanın karşılaştırması?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Dün en yüksek iade oranına sahip 5 ürün hangileri? Beden dağılımı nasıl?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Dün en yüksek iptal oranına sahip 5 ürün hangileri? Kanallar arasında iptal oranı farkı var mı?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Geçen haftaya göre büyüme veya düşüş hangi kategoride yaşandı? Trend devam ediyor mu?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Geçen yılın aynı ayıyla karşılaştırıldığında ciro, sipariş adedi ve sepet değeri nasıl değişti?", "agent": "eticaret", "importance": "orta"},
                    # Katalog/enrichment
                    {"question_text": "PIM'de yayında olup e-ticaret sitesinde görünmeyen ürünler var mı? Kaç adet?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Grade D ve F olan ürünlerin satış performansı nasıl? Grade artışının satışa etkisi görülüyor mu?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
            {
                "name": "Haftalık Performans",
                "frequency_type": "weekly",
                "schedule_time": "09:00",
                "active_days": [5],
                "tone": "analitik",
                "length": "detay",
                "top_insight_count": 4,
                "questions": [
                    {"question_text": "Bu hafta alınması gereken acil karar var mı? Kampanya değişikliği veya bütçe transferi gerekiyor mu?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Mevcut trend devam ederse bu ay kapanışta hedeften sapma ne kadar olur? Erken uyarı sinyal var mı?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Sezon sonuna kadar stok tükenme projeksiyonu: Hangi kategoride erken stok tükenme riski var?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Bu hafta enrichment yapılan ürün sayısı kaç? Grade A'ya yükselen ürünler hangileri?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
        ],
    },

    "urun_yoneticisi": {
        "name": "Ürün Yöneticisi",
        "role": "urun_yoneticisi",
        "tone": "operasyonel",
        "top_insight_count": 5,
        "schedules": [
            {
                "name": "Günlük Kalite Takibi",
                "frequency_type": "daily",
                "schedule_time": "09:00",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "operasyonel",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    {"question_text": "PIM'de yayında (internet aktif) olup e-ticaret sitesinde görünmeyen ürünler var mı? Kaç adet ve hangi kategorilerde?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Son 24 saatte PIM'de bloke edilen ürünler siteden gerçekten kalktı mı? Bloke öncesi satış rakamları neydi?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Uzun süredir (14 gün+) sıralamada değişiklik yapılmamış kategoriler hangileri? Hangi ürünler bu kategorilerde?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Yayında olup tüm renk/bedenleri bloke olan ürünler hangileri? Müşteri deneyimi etkisi nedir?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Bu hafta enrichment yapılan ürün sayısı kaç? Grade A'ya yükselen ürünler ve satışa etkileri?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Grade D ve F olan ürünlerin satış performansı nasıl? Grade artışının satışa etkisi görülüyor mu?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Ürün açıklaması boş veya çok kısa (100 karakterden az) olan yayındaki ürünler hangileri?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Kategori sıralamasında sürekli en altta kalan ve düşük satışlı ürünler hangileri? Aksiyon önerisi nedir?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Aktif ürünlerden son 7 günde hiç satışı olmayan kaç ürün var? En uzun süredir satışsız top 10 ürün?", "agent": "urun_yonetimi", "importance": "orta"},
                    {"question_text": "Aylık enrichment skoru ortalaması nedir? Grade dağılımı (A/B/C/D/F) geçen aya göre nasıl değişti?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
            {
                "name": "Haftalık Analiz",
                "frequency_type": "weekly",
                "schedule_time": "10:00",
                "active_days": [1],
                "tone": "analitik",
                "length": "detay",
                "top_insight_count": 4,
                "questions": [
                    {"question_text": "Bu hafta ekip üyesi bazında enrichment ve sıralama üretkenlik raporu: hedefler tutturuldu mu?", "agent": "kiyaslama", "importance": "kritik"},
                    {"question_text": "Dün içerik ekibi kaç ürün için zenginleştirme tamamladı? Hedeflenen ürün adedine ulaşıldı mı?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Dün kategori yönetimi ekibi kaç kategoride sıralama güncelledi? Günlük hedef tutturuldu mu?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Sezon bazında en iyi ve en kötü performanslı 20 ürün hangileri? Ortak başarı/başarısızlık faktörü?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
        ],
    },

    "eticaret_genel_mudur": {
        "name": "E-Ticaret Genel Müdür (SKL-01)",
        "role": "eticaret_genel_mudur",
        "tone": "yonetici",
        "top_insight_count": 5,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:30",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "yonetici",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    {"question_text": "Geçen ayın toplam e-ticaret cirası ve yıllık hedefin neresindeyiz? Sezon başından bu yana trend?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Brüt ciro ile net ciro arasındaki iade/iptal makası nedir? Geçen haftaya göre kötüleşme var mı?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Kanal bazında ciro payı ve WoW değişimi nasıl? Anormal düşüş veya artış gösteren kanal var mı?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Bu hafta toplam ciro hedefin neresindeyiz? Hangi kanal hedefi aştı?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Mevcut trend devam ederse bu ay kapanışta hedeften sapma ne kadar olur?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Stok kritik seviyeye düşen A ve B grade ürünler hangileri? Satış kaybı riski ne büyüklükte?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Geçen yılın aynı ayıyla ciro ve sepet değeri karşılaştırması nasıl?", "agent": "eticaret", "importance": "orta"},
                ],
            },
        ],
    },

    "eticaret_satis_ops": {
        "name": "Satış & Operasyon Müdürü (SKL-02)",
        "role": "eticaret_satis_ops",
        "tone": "operasyonel",
        "top_insight_count": 5,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:00",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "operasyonel",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    {"question_text": "En çok satan 10 ürün hangileri? Ciro ve adet bazında sıralama, geçen haftanın karşılaştırması?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Dün en yüksek iade oranına sahip 5 ürün hangileri? Beden bazında spike var mı?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Dün en yüksek iptal oranına sahip 5 ürün hangileri? Kanallar arası iptal farkı?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Brüt ciro ile net ciro arasındaki makas ne kadar? İade ve iptal oranları geçen haftaya göre nasıl?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Aktif ürünlerden son 7 günde hiç satışı olmayan kaç ürün var? Hangi kategorilerde yoğun?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "PIM'de yayında olup e-ticaret sitesinde görünmeyen ürünler var mı?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Geçen haftaya göre büyüme veya düşüş hangi kategoride yaşandı? Trend devam ediyor mu?", "agent": "eticaret", "importance": "orta"},
                    {"question_text": "Stok kritik seviyeye düşen A ve B grade ürünler hangileri?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
        ],
    },

    "eticaret_urun_katalog": {
        "name": "Ürün & Katalog Müdürü (SKL-03)",
        "role": "eticaret_urun_katalog",
        "tone": "operasyonel",
        "top_insight_count": 5,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "08:00",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "operasyonel",
                "length": "detay",
                "top_insight_count": 5,
                "questions": [
                    {"question_text": "PIM'de yayında olup e-ticaret sitesinde görünmeyen ürünler var mı? Kaç adet ve hangi kategorilerde?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Son 24 saatte PIM'de bloke edilen ürünler siteden kalktı mı?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Uzun süredir (14 gün+) sıralamada değişiklik yapılmamış kategoriler hangileri?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Yayında olup tüm renk/bedenleri bloke olan ürünler hangileri?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Aylık enrichment skoru ortalaması ve grade dağılımı (A/B/C/D/F) nasıl?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Bu hafta enrichment yapılan ürün sayısı kaç? Grade A'ya yükselen ürünler?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Grade D ve F olan ürünlerin satış performansı nasıl?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Ürün açıklaması boş veya çok kısa olan yayındaki ürünler kaç adet?", "agent": "urun_yonetimi", "importance": "orta"},
                    {"question_text": "Aktif ürünlerden son 7 günde hiç satışı olmayan kaç ürün var?", "agent": "urun_yonetimi", "importance": "orta"},
                ],
            },
        ],
    },

    "eticaret_pazarlama": {
        "name": "Pazarlama Müdürü (SKL-04)",
        "role": "eticaret_pazarlama",
        "tone": "analitik",
        "top_insight_count": 4,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "07:30",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "analitik",
                "length": "detay",
                "top_insight_count": 4,
                "questions": [
                    {"question_text": "Kanal bazında ciro payı ve WoW değişimi nasıl? Öne çıkan veya düşüş gösteren kanal?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Bu hafta toplam ciro hedefin neresindeyiz? Hangi kanal en fazla katkı sağlıyor?", "agent": "eticaret", "importance": "kritik"},
                    {"question_text": "Brüt ciro ile net ciro arasındaki makas ve iade oranları — kanal bazında en yüksek iade nereden geliyor?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Geçen haftaya göre büyüme veya düşüş hangi kategoride yaşandı?", "agent": "eticaret", "importance": "yuksek"},
                    {"question_text": "Geçen yılın aynı ayıyla karşılaştırıldığında ciro ve sipariş adedi nasıl değişti?", "agent": "eticaret", "importance": "orta"},
                    {"question_text": "Mevcut trend devam ederse bu ay kapanışta hedeften sapma ne kadar?", "agent": "kiyaslama", "importance": "yuksek"},
                ],
            },
        ],
    },

    "eticaret_teknoloji": {
        "name": "Teknoloji & Süreç Müdürü (SKL-05)",
        "role": "eticaret_teknoloji",
        "tone": "operasyonel",
        "top_insight_count": 4,
        "schedules": [
            {
                "name": "Günlük Brief",
                "frequency_type": "daily",
                "schedule_time": "06:30",
                "active_days": [1, 2, 3, 4, 5],
                "tone": "operasyonel",
                "length": "detay",
                "top_insight_count": 4,
                "questions": [
                    {"question_text": "PIM'de yayında olup e-ticaret sitesinde görünmeyen ürünler var mı? Teknik engel mi, veri sorunu mu?", "agent": "urun_yonetimi", "importance": "kritik"},
                    {"question_text": "Aylık enrichment skoru ortalaması ve grade dağılımı geçen aya göre nasıl değişti?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Uzun süredir sıralamada değişiklik yapılmamış kategoriler var mı? Pipeline durumu nasıl?", "agent": "urun_yonetimi", "importance": "yuksek"},
                    {"question_text": "Bu hafta ekip üyesi bazında enrichment ve sıralama üretkenlik hedefleri tutturuldu mu?", "agent": "kiyaslama", "importance": "yuksek"},
                    {"question_text": "Dün içerik ekibi kaç ürün enrichment tamamladı? Hedef ne kadardı?", "agent": "kiyaslama", "importance": "orta"},
                ],
            },
        ],
    },
}


def _t(s: str) -> dt_time:
    h, m = s.split(":")[:2]
    return dt_time(int(h), int(m))


async def seed(conn: asyncpg.Connection) -> None:
    created = 0
    updated = 0

    for key, tmpl in TEMPLATES.items():
        pid_key = f"seed_{key}"

        existing = await conn.fetchrow(
            "SELECT id FROM brief_profiles WHERE profile_id = $1", pid_key
        )

        if existing:
            profile_db_id = existing["id"]
            # Soruları ve zamanlamaları güncelle: sil + yeniden ekle
            schedule_ids = await conn.fetch(
                "SELECT id FROM brief_schedules WHERE profile_id = $1", profile_db_id
            )
            for sid in schedule_ids:
                await conn.execute("DELETE FROM brief_questions WHERE schedule_id = $1", sid["id"])
            await conn.execute("DELETE FROM brief_schedules WHERE profile_id = $1", profile_db_id)
            # top_insight_count güncelle
            await conn.execute(
                "UPDATE brief_profiles SET top_insight_count = $1 WHERE id = $2",
                tmpl.get("top_insight_count", 5), profile_db_id,
            )
            print(f"  UPD   {tmpl['name']} — zamanlamalar güncelleniyor")
            updated += 1
        else:
            profile_db_id = await conn.fetchval("""
                INSERT INTO brief_profiles
                    (profile_id, name, role, timezone, tone, top_insight_count, tenant_id)
                VALUES ($1, $2, $3, 'Europe/Istanbul', $4, $5, 'upagon')
                RETURNING id
            """, pid_key, tmpl["name"], tmpl.get("role", key),
                tmpl.get("tone", "yonetici"), tmpl.get("top_insight_count", 5))
            print(f"  OK    {tmpl['name']} — yeni oluşturuldu")
            created += 1

        for sched in tmpl["schedules"]:
            sched_id = await conn.fetchval("""
                INSERT INTO brief_schedules
                    (profile_id, name, frequency_type, schedule_time,
                     active_days, tone, length, format, top_insight_count, send_email)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'mixed', $8, true)
                RETURNING id
            """,
                profile_db_id,
                sched["name"],
                sched["frequency_type"],
                _t(sched.get("schedule_time", "07:00")),
                json.dumps(sched.get("active_days", [1, 2, 3, 4, 5])),
                sched.get("tone", tmpl.get("tone", "yonetici")),
                sched.get("length", "detay"),
                sched.get("top_insight_count", tmpl.get("top_insight_count", 5)),
            )

            for i, q in enumerate(sched.get("questions", [])):
                await conn.execute("""
                    INSERT INTO brief_questions
                        (schedule_id, question_text, agent, importance,
                         is_cross_domain, sort_order)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """,
                    sched_id,
                    q["question_text"],
                    q["agent"],
                    q.get("importance", "orta"),
                    q.get("is_cross_domain", False),
                    i,
                )

            q_count = len(sched.get("questions", []))
            print(f"        ↳ {sched['name']} — {q_count} soru")

    print(f"\nSeed tamamlandı: {created} yeni, {updated} güncellendi.")


async def main() -> None:
    conn = await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "pimland"),
        password=os.getenv("POSTGRES_PASSWORD"),
        database=os.getenv("POSTGRES_DB", "pimland_reporting"),
    )
    print("Şablon profiller güncelleniyor...\n")
    async with conn.transaction():
        await seed(conn)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
