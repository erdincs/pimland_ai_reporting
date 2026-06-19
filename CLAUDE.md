# Pimland AI Reporting — Teknik Referans

## Mühendislik Kuralları — Her Değişiklikte Uygula

Bu kurallar proje genelinde tespit edilen yapısal sorunlardan türetilmiştir.
Yeni kod yazarken, mevcut kodu değiştirirken veya PR hazırlarken bu kuralları kontrol et.

### KURAL 1 · Fail Fast — Sessiz Hata Yasak

Bir şey çalışmıyorsa, en erken noktada patla. Exception yutup boş veri dönme.

```
❌ YANLIŞ: try/except → return []   (agent boş veri ile devam eder, kullanıcı nedenini bilmez)
✅ DOĞRU:  try/except → log.error + açık hata mesajı dön + fallback varsa belirt
```

- Startup'ta tüm connector health check'leri çalışmalı
- Tablo/view yoksa agent sessizce boş dönmemeli, "X tablosu mevcut değil" demeli
- Auth hatası "connection timeout" olarak maskelenmemeli

### KURAL 2 · Tek Kaynak (SSOT) — Aynı Bilgi Tek Yerde

Bir tanım birden fazla yerde varsa, tutarsızlık kaçınılmazdır.

| Bilgi | Tek Kaynak | Türetilenler |
|-------|------------|--------------|
| Tablo şeması | Alembic migration | main.py DDL kaldırılacak |
| View tanımı | `sync/config/views.yaml` | `refresh_views.py` buradan okur |
| View SQL'i | `scripts/materialized_views_v2.sql` | views.yaml description referans verir |
| Connector config | `config/sources/*.yaml` | `sync/config/sources.yaml` eski sistem, taşınacak |
| Agent veri bağımlılığı | Agent dosyasının başındaki docstring | Schema contract ile doğrulanır |

Yeni view/tablo eklerken: migration + views.yaml + SQL dosyası + agent docstring — hepsini güncelle.

### KURAL 3 · Data Contract — Agent Ne Bekliyorsa, O Var Olmalı

Her agent dosyasının başında beklediği tablo ve view'lar belgelidir.
Bu bağımlılıklar startup'ta veya test'te doğrulanmalıdır.

```python
# Agent dosyasının başındaki docstring'te:
"""Kaynak: mv_magaza_satis_ozet (hızlı) → incorta_magaza_performans (fallback)."""

# Bu, şu anlama gelir:
# 1. mv_magaza_satis_ozet view'ı tanımlı olmalı (views.yaml + SQL dosyası)
# 2. incorta_magaza_performans tablosu DDL'de olmalı
# 3. Her ikisi de validate.py'da kontrol edilmeli
```

Yeni agent yazarken veya mevcut agent'a veri kaynağı eklerken:
- Kaynak tablo/view gerçekten var mı? (`\dt` veya `\dm` ile kontrol et)
- views.yaml'da listelendi mi?
- validate.py kontrol listesine eklendi mi?

### KURAL 4 · Graceful Degradation Zinciri — Her Veri Erişiminde Fallback

Her veri çekme noktasında en az 2 katmanlı fallback olmalı:

```
1. Hızlı yol   → Materialized view (önbellekli, hızlı)
2. Yavaş yol   → Ham tablo (her zaman mevcut, yavaş)
3. Hata yolu   → Açık mesaj: "X verisi şu an kullanılamıyor: [neden]"
```

```
❌ YANLIŞ: Doğrudan tek tabloya sorgu, try/except yok
✅ DOĞRU:  magaza_agent.py deseni — view → ham tablo → açık hata mesajı
```

`eticaret_kpi.py` gibi doğrudan günlük tabloya sorgu atan kodlar bu zincire alınmalı.

### KURAL 5 · İki Sync Sistemi Farkındalığı

Projede iki ayrı sync altyapısı var. Yeni sync işi eklerken hangi sistemi kullandığını bilinçli seç.

| | Sistem A — Lambda Sync | Sistem B — Connector Framework |
|---|---|---|
| Dizin | `sync/` | `app/connectors/` + `config/sources/` |
| Config | `sync/config/sources.yaml` | `config/sources/*.yaml` |
| Runtime | AWS Lambda + EventBridge | APScheduler in-process |
| DB write | psycopg2 batch | pandas + SQLAlchemy |
| Log tablosu | `sync_log` | `sync_jobs` |
| Auth | Request body (`Authorization` key) | Config'e bağlı (header veya body) |
| Test | ✅ 15 unit test | ❌ Eksik — her yeni connector'a test yaz |

**Hedef:** Sistem A'yı Sistem B'ye taşımak (strangler fig). Yeni sync işleri Sistem B'de yazılır.

**DİKKAT — Incorta Auth:** Incorta MCP endpoint'leri `Authorization` token'ı HTTP header'da değil, request body'de bekler. Connector YAML'larında `tool.args.Authorization` kullan:

```yaml
tool:
  args:
    Authorization: "__env:INCORTA_TOKEN"
```

### KURAL 6 · Test Zorunluluğu — Değiştirdiğin Katmanı Test Et

| Katman | Minimum test |
|--------|-------------|
| Yeni connector | fetch + auth + pagination + error handling (4 test) |
| Yeni agent | veri çekme + boş veri + fallback + A2A sinyal (4 test) |
| Yeni sync job | başarılı sync + kısmi hata + tam hata (3 test) |
| Yeni view | SQL doğruluk + index varlığı (2 test) |
| Yeni endpoint | 200 OK + 404 + validation error (3 test) |

```bash
# Test çalıştırma
PYTHONPATH=. pytest tests/ -v
```

### KURAL 7 · Validation Kapsamı — Yeni Tablo = Yeni Kontrol

`sync/jobs/validate.py` ve `app/connectors/sync_pipeline.py` tüm aktif tabloları kontrol etmeli. Yeni tablo eklerken:

1. `validate.py` → `_TABLOLAR` listesine ekle
2. Günlük tablo ise → "dünün verisi var mı?" kontrolü ekle
3. `sync_pipeline.py` → `_VIEW_DEPENDENCIES`'e bağımlılık ekle

### KURAL 8 · View Ekleme Kontrol Listesi

Yeni materialized view eklerken bu sırayı takip et:

```
□ SQL tanımı → scripts/materialized_views_v2.sql
□ UNIQUE INDEX → CONCURRENTLY refresh için gerekli
□ views.yaml → depends_on ve order tanımla
□ refresh_views.py → _UNIQUE_INDEXES dict'ine ekle (gerekiyorsa)
□ Agent docstring → hangi agent bu view'ı kullanıyor?
□ validate.py → view varlık kontrolü (opsiyonel)
□ DB'de çalıştır → CREATE MATERIALIZED VIEW ...
```

### KURAL 9 · Monitoring Birliği

İki log tablosu (`sync_log` + `sync_jobs`) olduğunu unutma.
`sync/jobs/daily_report.py` sadece `sync_log`'dan okuyor.
Connector framework hataları `sync_jobs`'a yazılıyor.

Monitoring eklerken her iki tabloyu da kontrol et, yoksa günlük raporlar
connector hatalarını göstermez.

### KURAL 10 · LLM Çağrısı Kuralları

- Bedrock Converse API kullan (boto3), anthropic SDK yok
- Model: `eu.anthropic.claude-sonnet-4-6` (inference profile)
- httpx sürümü 0.27.2 sabit — 0.28+ boto3 ile çakışır, güncelleme
- Agent timeout: 3sn (paralel), 180sn (brief orchestrator toplu)
- Halüsinasyon koruması: sub-agent vermediği sayıyı söyleme
- Türkçe, yönetici tonu, jargon yok, "tahmini/yaklaşık" etiketleri zorunlu

---

## Skills

| Skill | Ne zaman okunur |
|-------|----------------|
| **pimland-design-system** | HTML/CSS/dashboard/KPI kartı/artifact yazarken |
| **pimland-report-generator** | SQL, veri analizi, rapor pipeline yazarken |
| **urun-yonetimi-dashboard** | PLM katalog, marka/sezon/kategori drill-down yazarken |
| **adl-rapor-yonetici** | "yönetici özeti", "sabah raporu", "CEO brief", "günlük brief", "executive summary", "konsolide rapor" ifadelerinde |
| **adl-rapor-eticaret** | "e-ticaret raporu", "kanal analizi", "Trendyol raporu", "iade matrisi", "GA4 trafik", "tam fiyat analizi" ifadelerinde |
| **adl-rapor-magaza** | "mağaza raporu", "bölge müdürü", "MDO analizi", "same-store sales", "LfL büyüme", "kritik mağaza" ifadelerinde |
| **adl-rapor-premium** | "marka sağlığı", "premium analiz", "fiyat disiplini", "brand health", "sezon performans", "kategori mix" ifadelerinde |
| **adl-rapor-urun-stok** | "stok analizi", "stok yaşı", "dead stock", "sell-through", "restock önerisi", "beden tükenme" ifadelerinde |

---

## Stack

| Katman | Teknoloji |
|--------|-----------|
| API | FastAPI 0.115, Uvicorn, Python 3.11 |
| ORM | SQLAlchemy 2.0 async (asyncpg), Alembic |
| Cache | Redis 7 |
| LLM | AWS Bedrock — boto3 Converse API (anthropic SDK yok) |
| Model | `arn:aws:bedrock:eu-north-1:448049806345:inference-profile/eu.anthropic.claude-sonnet-4-6` |
| HTTP client | httpx **0.27.2** (sabit — 0.28+ boto3 ile çakışır) |
| SQL guard | sqlglot |
| Scheduler | APScheduler |
| Container | Docker + Docker Compose |

---

## Yerel Geliştirme

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

| Servis | URL |
|--------|-----|
| Portal SPA | http://localhost:8000/api/v1/portal |
| Swagger | http://localhost:8000/docs |
| NL Sorgu | POST http://localhost:8000/api/v1/query |
| Health | http://localhost:8000/api/v1/health |

Portal şifresi: `pimland2026`

Env değişkenleri için `.env.example`'dan `.env` oluştur.

---

## Deploy — AWS EC2

### Sunucu

| Alan | Değer |
|------|-------|
| IP | `56.228.8.236` |
| Instance ID | `i-03777b6c7279662e6` |
| Tür | t3.small, Amazon Linux 2023, eu-north-1 |
| Kullanıcı | `ec2-user` |
| Proje dizini | `/opt/pimland-reporting/app/` |
| SSH key pair | `pimland-key` (PEM kaybolmuşsa Instance Connect kullan) |

### SSH — EC2 Instance Connect (PEM gerektirmez)

```bash
ssh-keygen -t rsa -b 2048 -f /tmp/ec2_key -N ""

AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
  aws ec2-instance-connect send-ssh-public-key \
  --region eu-north-1 \
  --instance-id i-03777b6c7279662e6 \
  --instance-os-user ec2-user \
  --ssh-public-key file:///tmp/ec2_key.pub

# Hemen bağlan (60 saniyelik pencere):
ssh -i /tmp/ec2_key -o StrictHostKeyChecking=no ec2-user@56.228.8.236
```

### Docker servisleri

| Container | Image | Port |
|-----------|-------|------|
| app-api-1 | app-api:latest | 8080→8000 |
| app-db-1 | postgres:14-alpine | 5432 (iç) |
| app-redis-1 | redis:7-alpine | 6379 (iç) |

### Rebuild + deploy

```bash
# EC2'de:
cd /opt/pimland-reporting/app
DOCKER_BUILDKIT=0 docker build -t app-api:latest .
docker compose -f docker-compose.prod.yml up -d --no-deps api
```

### Hızlı deploy (yalnızca Python/HTML değiştiyse)

```bash
# 1. Instance Connect key gönder (yukarıdaki adım)
# 2. Dosyaları kopyala (proje kökünden):
scp -i /tmp/ec2_key app/static/portal.html \
  ec2-user@56.228.8.236:/opt/pimland-reporting/app/app/static/portal.html

# 3. EC2'de rebuild:
ssh -i /tmp/ec2_key ec2-user@56.228.8.236 \
  "cd /opt/pimland-reporting/app && DOCKER_BUILDKIT=0 docker build -t app-api:latest . -q && \
   docker compose -f docker-compose.prod.yml up -d --no-deps api"
```

### Prod env dosyası

`/opt/pimland-reporting/app/.env.prod` — şunları içermeli:
`POSTGRES_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BEDROCK_MODEL_ID`, `PIMLAND_*`, `INCORTA_TOKEN`

---

## Önemli Dosyalar

### Çekirdek
| Dosya | Ne yapar |
|-------|----------|
| `app/main.py` | FastAPI app factory, lifespan |
| `app/core/config.py` | Tüm env değişkenleri, DSN'ler |
| `app/api/v1/router.py` | Router birleştirici |
| `app/db/session.py` | AsyncSession factory |

### Agent & LLM
| Dosya | Ne yapar |
|-------|----------|
| `app/agent/llm_client.py` | boto3 Bedrock Converse — multi-turn history |
| `app/agent/schema_context.py` | LLM'e gönderilen DB şeması |
| `app/agent/prompts/text_to_sql.py` | SQL üretim system prompt |
| `app/services/sql_guard.py` | sqlglot SQL güvenlik katmanı |

### AI Agent servisleri
| Dosya | Ne yapar |
|-------|----------|
| `app/services/callcenter_service.py` | Call Center Agent |
| `app/services/sizewin_service.py` | Sizewin Agent |
| `app/connectors/pimland_live.py` | Pimland MCP live client (7 paralel araç) |

### Portal & Raporlar
| Dosya | Ne yapar |
|-------|----------|
| `app/static/portal.html` | SPA — tüm UI (dark/light, 3 agent, drill-down) |
| `app/reports/portal_queries.py` | Portal SQL sorguları |
| `app/api/v1/endpoints/portal.py` | Portal endpoint'leri |
| `app/api/v1/endpoints/agents.py` | Agent endpoint'leri |

### Raporlama Agent'ları
| Dosya | Ne yapar | Veri kaynağı |
|-------|----------|-------------|
| `app/services/reporting/magaza_agent.py` | Fiziksel mağaza satış analizi | mv_magaza_satis_ozet → incorta_magaza_performans |
| `app/services/reporting/eticaret_agent.py` | E-ticaret kanal/SKU analizi | mv_net_satis_* + incorta_ecommerce_gunluk |
| `app/services/reporting/kiyaslama_agent.py` | YoY/MoM karşılaştırma (yaprak) | Tüm mv_* view'lar |
| `app/services/reporting/enrichment_agent.py` | Ürün kalite puanı (yaprak) | enrichment_quality |
| `app/services/reporting/siralama_agent.py` | Kategori sıralama analizi | siralama_gecmisi |
| `app/services/reporting/orchestrator.py` | Agent routing + A2A koordinasyon | — |

### Daily Brief
| Dosya | Ne yapar |
|-------|----------|
| `app/services/daily_brief/orchestrator.py` | Brief üretim — paralel agent çağrısı, DB'ye yazma |
| `app/services/daily_brief/composer.py` | Claude ile brief sentezleme |
| `app/services/daily_brief/eticaret_kpi.py` | E-ticaret KPI hesaplama (⚠️ fallback yok) |

### Sync — Sistem A (Lambda)
| Dosya | Ne yapar |
|-------|----------|
| `sync/orchestrator.py` | Lambda job koordinasyon, sync_log |
| `sync/sources/incorta_sync.py` | Incorta MCP → RDS (aylık tablolar) |
| `sync/sources/pimland_sync.py` | Pimland MCP → RDS (ürün kataloğu) |
| `sync/jobs/refresh_views.py` | 7 materialized view sıralı refresh |
| `sync/jobs/validate.py` | Orphan SKU, boş tablo, ciro sağlık |
| `sync/config/sources.yaml` | 4 Incorta tablo + Pimland config |
| `sync/config/views.yaml` | View tanımları + bağımlılık sırası |

### Sync — Sistem B (Connector Framework)
| Dosya | Ne yapar |
|-------|----------|
| `app/connectors/registry.py` | config/sources/*.yaml yükleyici |
| `app/connectors/mcp_client.py` | Async MCP client (tenacity retry) |
| `app/connectors/sync_pipeline.py` | fetch → normalize → bulk load → view refresh |
| `app/services/scheduler.py` | APScheduler cron job registration |
| `config/sources/*.yaml` | 19 veri kaynağı config (günlük dahil) |

---

## Veri Kaynakları

### Aktif — Çalışan Tablolar
| Tablo | ~Satır | Sync | Açıklama |
|-------|--------|------|----------|
| `incorta_satis` | 505K | Connector (Sistem B) | Brüt satışlar (aylık) |
| `incorta_depo_iade` | 216K | Lambda (Sistem A) | İadeler (negatif tutar/adet) |
| `incorta_iptal_siparis` | 48K | Lambda (Sistem A) | İptaller (negatif tutar/adet) |
| `incorta_analytics` | 87K | Lambda (Sistem A) | Web analytics (GA4) |
| `incorta_ecommerce_gunluk` | ~851K | Connector (Sistem B) | E-ticaret günlük SKU/kanal/tutar |
| `incorta_magaza_performans` | ~1K/ay | Connector (Sistem B) | Mağaza KPI (aylık) |
| `pim_products` | 4140 | Lambda (Sistem A) | PLM ürün kataloğu |

### Streaming Tabloları (OOM koruması)
`app/connectors/sync_pipeline.py` → `_STREAMING_TABLES` — bu tablolar her 20 sayfada (100K satır) DB'ye yazılır, bellekte tutulmaz.

| Tablo | Satır | Neden streaming |
|-------|-------|----------------|
| `incorta_ecommerce_gunluk` | ~851K | t3.small 2GB RAM → OOM riski |
| `incorta_satis` | ~505K | t3.small 2GB RAM → OOM riski |

**DİKKAT:** `_STREAMING_TABLES` target_table adlarını içerir (source_id değil). `incorta_ecommerce_sales` source'unun target_table'ı `incorta_satis`'tir.

### Materialized Views
| View | Kaynak | Durum |
|------|--------|-------|
| `mv_net_satis_aylik` | incorta_satis + iade + iptal | ✅ Aktif |
| `mv_net_satis_urun` | incorta_satis + iade + iptal | ✅ Aktif |
| `mv_net_satis_kanal` | incorta_satis + iade + iptal | ✅ Aktif |
| `mv_satis_marka_sezon` | incorta_satis + pim_products | ✅ Aktif |
| `mv_satis_kategori` | incorta_satis + pim_products | ✅ Aktif |
| `mv_analytics_kanal` | incorta_analytics | ✅ Aktif |
| `mv_analytics_gunluk` | incorta_analytics | ✅ Aktif |
| `mv_magaza_satis_ozet` | incorta_magaza_performans | ⚠️ Agent referans veriyor, DDL/SQL yok |
| `mv_ecom_gunluk` | incorta_ecommerce_gunluk | ✅ Aktif (gun × satis_kanali) |
| `mv_ecom_haftalik` | incorta_ecommerce_gunluk | ✅ Aktif (yil × hafta × satis_kanali) |

Ürün görseli: `https://img-adl.sm.mncdn.com/cdnimages/products/{urun_kodu}_{first_color_code}_1.jpg`
