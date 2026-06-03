# Pimland AI Reporting — Teknik Referans

## Skills

| Skill | Ne zaman okunur |
|-------|----------------|
| **pimland-design-system** | HTML/CSS/dashboard/KPI kartı/artifact yazarken |
| **pimland-report-generator** | SQL, veri analizi, rapor pipeline yazarken |
| **urun-yonetimi-dashboard** | PLM katalog, marka/sezon/kategori drill-down yazarken |

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
| app-api-1 | app-api:latest | 80→8000 |
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

### Connector & Config
| Dosya | Ne yapar |
|-------|----------|
| `app/connectors/registry.py` | YAML'dan connector yükler |
| `app/services/scheduler.py` | Gece sync job'ları |
| `config/sources/*.yaml` | Veri kaynağı bağlantı + schedule config |

---

## Veri Kaynakları

| Tablo | ~Satır | Açıklama |
|-------|--------|----------|
| `incorta_satis` | 108K | Brüt satışlar |
| `incorta_depo_iade` | 45K | İadeler (negatif tutar/adet) |
| `incorta_iptal_siparis` | 10K | İptaller (negatif tutar/adet) |
| `incorta_analytics` | 24K | Web analytics |
| `pim_products` | 4140 | PLM ürün kataloğu |

Ürün görseli: `https://img-adl.sm.mncdn.com/cdnimages/products/{urun_kodu}_{first_color_code}_1.jpg`
