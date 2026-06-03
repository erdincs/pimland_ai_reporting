# Pimland AI Reporting — Claude Code Rehberi

## Skills — Her zaman oku

Bu projede üç skill tanımlıdır. **İlgili görevde mutlaka oku:**

| Skill | Ne zaman okunur |
|-------|----------------|
| **pimland-design-system** | HTML/CSS/dashboard/artifact/KPI kartı yazarken |
| **pimland-report-generator** | SQL sorgusu, veri analizi, rapor pipeline'ı yazarken |
| **urun-yonetimi-dashboard** | PLM katalog, marka/sezon/kategori/drill-down dashboard yazarken |

---

## 1. Proje Özeti

Pimland PLM sistemi için **FastAPI tabanlı AI raporlama modülü**. Kullanıcılar doğal dil (Türkçe) ile e-ticaret satış verilerini sorgular; sistem SQL üretir, DB hesaplar, özet sonucu Claude'a iletir.

Ayrıca üç AI agent içerir:
- **Pimland AI Agent** — doğal dil → SQL → yanıt
- **Call Center Agent** — çağrı merkezi çalışanları için ürün bilgi asistanı
- **Sizewin Agent** — müşteri ölçülerine göre beden önerisi

**Stack:**
- Python 3.11, FastAPI 0.115, Uvicorn (ASGI)
- PostgreSQL 14 (asyncpg + psycopg2), SQLAlchemy 2.0 async, Alembic
- Redis 7 (önbellek, oturum)
- AWS Bedrock — boto3 Converse API, model: `eu.anthropic.claude-sonnet-4-6` (eu-north-1 inference profile)
- AWS S3 (raw dosya arşivi)
- Docker + Docker Compose (prod)
- sqlglot (SQL güvenlik parsing)
- APScheduler (gece sync job'ları)
- httpx 0.27.2 (Pimland MCP live client — 0.28+ boto3'ü kırar, sabit tut)

---

## 2. Temel Mimari Kurallar

1. **Agent ham veriyi ASLA görmez** — SQL üretir, DB hesaplar, sadece özet (≤200 satır) LLM'e döner
2. **incorta_satis ana kaynak** — `eticaret_satis` eski (Excel), tüm yeni sorgular `incorta_satis` kullanır
3. **Materialized view önce** — `mv_satis_*` view'lar `incorta_satis`'ten daha hızlıdır
4. **Read-only DB** — tüm sorgular `pimland_ro` rolüyle `AsyncSession` üzerinden çalışır
5. **İki katmanlı SQL güvenliği** — (1) read-only DB rolü, (2) sqlglot tabanlı `sql_guard.py`
6. **Türkçe yanıt** — kullanıcı Türkçe sorarsa Türkçe yanıt ver
7. **httpx sabitle** — `httpx==0.27.2` (0.28+ boto3 sızdırır, yükseltme)
8. **PostgreSQL ROUND cast** — `ROUND(expr::numeric, 2)` kullan; `double precision` ile çalışmaz

---

## 3. Yerel Geliştirme

### Ön koşullar
- Python 3.11+
- PostgreSQL 14 çalışıyor (lokal veya Docker)
- Redis çalışıyor
- `.env` dosyası doldurulmuş (`.env.example`'dan kopyala)

### Kurulum
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# DB migration
alembic upgrade head

# Sunucuyu başlat
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Tek komutla (venv aktifse)
```bash
PYTHONPATH=. .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Lokal URL'ler
| Servis | Adres |
|--------|-------|
| Portal SPA | http://localhost:8000/api/v1/portal |
| Swagger | http://localhost:8000/docs |
| NL Sorgu | POST http://localhost:8000/api/v1/query |
| Health | http://localhost:8000/api/v1/health |

### Portal şifresi
`pimland2026`

---

## 4. Deploy Süreci (AWS EC2)

### Sunucu bilgileri
| Alan | Değer |
|------|-------|
| IP | `56.228.8.236` |
| Bölge | eu-north-1 (Stockholm) |
| Instance | i-03777b6c7279662e6 (t3.small) |
| OS | Amazon Linux 2023 |
| Kullanıcı | `ec2-user` |
| Proje dizini | `/opt/pimland-reporting/app/` |
| SSH key pair | `pimland-key` |

**Not:** SSH key (`.pem` dosyası) lokal makinede yoksa AWS EC2 Instance Connect kullanılır — PEM gerektirmez.

### SSH bağlantısı (EC2 Instance Connect ile)
```bash
# 1. Geçici key oluştur
ssh-keygen -t rsa -b 2048 -f /tmp/ec2_temp_key -N ""

# 2. Key'i EC2'ye gönder (60 saniyelik pencere)
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
  aws ec2-instance-connect send-ssh-public-key \
  --region eu-north-1 \
  --instance-id i-03777b6c7279662e6 \
  --instance-os-user ec2-user \
  --ssh-public-key file:///tmp/ec2_temp_key.pub

# 3. Hemen bağlan (60 saniye içinde)
ssh -i /tmp/ec2_temp_key -o StrictHostKeyChecking=no ec2-user@56.228.8.236
```

### Manuel deploy (tam rebuild)
```bash
# EC2'de çalıştır:
cd /opt/pimland-reporting/app
DOCKER_BUILDKIT=0 docker build -t app-api:latest .
docker compose -f docker-compose.prod.yml up -d --no-deps api
```

### Hızlı deploy (yalnızca Python dosyaları değiştiyse)
```bash
# 1. Lokal'den dosyaları kopyala (key gönderildikten hemen sonra):
scp -i /tmp/ec2_temp_key app/static/portal.html \
  ec2-user@56.228.8.236:/opt/pimland-reporting/app/app/static/portal.html

# 2. EC2'de rebuild + restart:
ssh -i /tmp/ec2_temp_key ec2-user@56.228.8.236 \
  "cd /opt/pimland-reporting/app && DOCKER_BUILDKIT=0 docker build -t app-api:latest . -q && docker compose -f docker-compose.prod.yml up -d --no-deps api"
```

### Docker servisleri
| Container | Image | Port |
|-----------|-------|------|
| app-api-1 | app-api:latest | 80→8000 |
| app-db-1 | postgres:14-alpine | 5432 (internal) |
| app-redis-1 | redis:7-alpine | 6379 (internal) |

### Ortam değişkenleri
Production'da `/opt/pimland-reporting/app/.env.prod` kullanılır. Şunları içermeli:
- `POSTGRES_PASSWORD`, `POSTGRES_READONLY_PASSWORD`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (veya instance role — tercih edilir)
- `BEDROCK_MODEL_ID` (ARN formatında)
- `PIMLAND_*` (MCP OAuth2 kimlik bilgileri)
- `INCORTA_TOKEN`

---

## 5. Mimari Kararlar

### Neden AWS Bedrock, Anthropic SDK değil?
boto3 Converse API model-agnostik; aynı kod Claude/Titan/Llama'ya çalışır. Ayrıca kurumsal VPC içinde kalır, veri Anthropic'e gitmez. Kimlik bilgisi yoksa instance role devralır.

### Neden Text-to-SQL, RAG değil?
Veriler yapısal (PostgreSQL tablolar). Belgeler için RAG mantıklı; sayısal e-ticaret analizi için SQL çok daha kesin ve kontrol edilebilir. `sql_guard.py` + read-only rol ile güvenlik sağlanır.

### Neden tek HTML dosyası (portal.html)?
Build araçları gerektirmez, EC2'ye tek `scp` ile deploy edilir. SPA davranışı inline JS ile sağlanır. Dezavantaj: büyük dosya — gelecekte component mimariye geçilebilir.

### Neden materialized view?
`incorta_satis` (108K satır) üzerinde her sorguda GROUP BY yerine `mv_satis_*` view'ları önceden hesaplanmış özetler tutar. Portal KPI'ları için 10-50x daha hızlı.

### Neden iki read-only katmanı?
(1) `pimland_ro` PostgreSQL rolü — DML fiziksel olarak imkânsız.
(2) `sql_guard.py` sqlglot parsing — prompt injection ile CTE içine gizlenmiş DML'i de yakalar.

### Konuşma hafızası (Agent'lar)
Her agent bağımsız HTTP isteğidir. Frontend `agentHistory` dizisinde `{role, content}` turlarını saklar, her istekte `history` alanıyla gönderir. LLM client bunu Bedrock `messages` listesine çevirir. Max 20 tur (40 mesaj) tutulur.

---

## 6. Önemli Dosyalar

### Uygulama çekirdeği
| Dosya | Ne yapar |
|-------|----------|
| `app/main.py` | FastAPI app factory, lifespan (scheduler + registry) |
| `app/core/config.py` | Tüm env değişkenleri, DSN'ler (pydantic-settings) |
| `app/api/v1/router.py` | Tüm endpoint router'larını birleştirir |
| `app/db/session.py` | AsyncSession factory, read-only session |

### Agent & SQL
| Dosya | Ne yapar |
|-------|----------|
| `app/agent/llm_client.py` | boto3 Bedrock Converse wrapper, multi-turn history desteği |
| `app/agent/schema_context.py` | LLM'e gönderilen DB şeması kataloğu |
| `app/agent/prompts/text_to_sql.py` | SQL üretim system prompt'u |
| `app/agent/text_to_sql.py` | Text→SQL pipeline |
| `app/services/sql_guard.py` | sqlglot tabanlı SQL güvenlik katmanı |
| `app/services/query_service.py` | NL sorgu orkestrasyonu |

### AI Agent servisleri
| Dosya | Ne yapar |
|-------|----------|
| `app/services/callcenter_service.py` | Call Center Agent — sistem prompt, DB+MCP paralel fetch |
| `app/services/sizewin_service.py` | Sizewin Agent — beden öneri, iade analizi, beden tabloları |
| `app/connectors/pimland_live.py` | Pimland MCP live client (7 paralel araç, OAuth2) |

### Portal & Raporlar
| Dosya | Ne yapar |
|-------|----------|
| `app/static/portal.html` | Tek dosya SPA — tüm UI (dark/light, 3 agent, drill-down) |
| `app/reports/portal_queries.py` | Portal endpoint'leri için SQL sorguları |
| `app/reports/ecommerce_monthly.py` | Aylık HTML rapor üretici |
| `app/api/v1/endpoints/portal.py` | Portal REST endpoint'leri |
| `app/api/v1/endpoints/agents.py` | Agent endpoint'leri (callcenter, sizewin) |

### Connector altyapısı
| Dosya | Ne yapar |
|-------|----------|
| `app/connectors/registry.py` | YAML config'lerden connector yükler |
| `app/connectors/mcp_client.py` | Incorta MCP sync client |
| `app/services/scheduler.py` | APScheduler — gece sync job'ları |
| `config/sources/*.yaml` | Her veri kaynağı için bağlantı + schedule konfigürasyonu |

### Deployment
| Dosya | Ne yapar |
|-------|----------|
| `Dockerfile` | Multi-stage Python 3.11-slim image |
| `docker-compose.prod.yml` | Production stack (api + db + redis) |
| `scripts/deploy.sh` | EC2 üzerinde tam deployment scripti |
| `.env.prod` | Production ortam değişkenleri (commit'leme) |

---

## 7. Veri Kaynakları

| Tablo | Satır | Açıklama |
|-------|-------|----------|
| `incorta_satis` | ~108K | Brüt e-ticaret satışları (ana kaynak) |
| `incorta_depo_iade` | ~45K | Depoya dönen iadeler (tutar/adet negatif) |
| `incorta_iptal_siparis` | ~10K | İptal siparişler (tutar/adet negatif) |
| `incorta_analytics` | ~24K | Web analytics (trafik, conversion) |
| `pim_products` | ~4140 | PLM ürün kataloğu |
| `pim_brands` | 3 | Marka master (ADL, Love My Body, Night Zoom) |
| `pim_colors` | 241 | Renk master |
| `pim_seasons` | 35 | Sezon master |
| `pim_product_groups` | 59 | Ürün grubu master |

**Net ciro formülü:** `SUM(satis.tutar) + SUM(iade.tutar) + SUM(iptal.tutar)` — iade ve iptal zaten negatif gelir.

**Ürün görseli URL formatı:**
```
https://img-adl.sm.mncdn.com/cdnimages/products/{urun_kodu}_{first_color_code}_1.jpg
```

---

## 8. Sık Karşılaşılan Hatalar

| Hata | Neden | Çözüm |
|------|-------|-------|
| `function round(double precision, integer) does not exist` | PostgreSQL'de double precision ROUND'u 2 argüman almaz | `ROUND(expr::numeric, 2)` kullan |
| `Identifier 'X' has already been declared` | Aynı fonksiyonda `const` iki kez tanımlanmış | İkincisini farklı isimle yeniden adlandır |
| `ModuleNotFoundError: No module named 'httpx'` | requirements.txt'te eksik | `httpx==0.27.2` ekle; 0.28+ sürüm boto3'ü kırar |
| `InvalidColumnReferenceError` | `SELECT DISTINCT col1 ORDER BY col2` — DISTINCT ile ORDER BY başka sütun olamaz | Subquery'ye sar: `SELECT col1 FROM (SELECT DISTINCT col1, col2 ...) s ORDER BY col2` |
| `asyncio.coroutine_noop()` yok | Var olmayan asyncio API | `if/else` ile paralel veya sıralı akış yaz |
