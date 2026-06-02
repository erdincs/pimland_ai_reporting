# Pimland AI Reporting

Doğal dil ile analiz yapılmasını sağlayan, Pimland PLM sistemine entegre AI
raporlama modülü. Kullanıcı Türkçe/İngilizce soru sorar; sistem SQL üretir,
veritabanı hesaplar, LLM yalnızca **özet** sonucu yorumlar.

## Mimari prensip — Agent ham veriyi görmez

```
Soru + filtreler
   │
   ├─► cache (Redis) ──── hit ─► yanıt
   │
   ├─► text-to-SQL agent      (yalnızca şema görür, veri görmez)
   ├─► SQL guard              (read-only doğrulama + LIMIT)
   ├─► execute                (read-only DB rolü, hesabı DB yapar)
   ├─► analyzer               (yalnızca küçük özet sonucu görür)
   └─► cache + yanıt
```

Bu ayrım maliyet ve güvenliğin temelidir: büyük veri kümeleri asla LLM'e
gönderilmez; üretilen SQL hem en az yetkili DB rolüyle hem de `sql_guard`
katmanıyla iki kez korunur.

## Klasör yapısı

```
app/
├── core/         # config, logging, exceptions  (çapraz kesen altyapı)
├── api/v1/       # FastAPI router + endpoints (query, ingestion, health)
├── db/           # async engine'ler, session'lar, ORM base
├── schemas/      # Pydantic istek/yanıt DTO'ları
├── agent/        # LLM client, text-to-SQL, schema context, analyzer, prompts
├── services/     # query_service (orkestrasyon), sql_guard, cache_service
├── ingestion/    # Excel → Postgres pipeline
└── rag/          # (ileride) ürün dökümanları için retrieval
migrations/       # Alembic
scripts/          # load_excel.py, materialized_views.sql
tests/            # unit + integration
```

## Hızlı başlangıç

```bash
cp .env.example .env          # değerleri doldur (ANTHROPIC_API_KEY vb.)
make up                       # Postgres + Redis + API (Docker)
# veya yerel:
make dev && make run
```

API dokümantasyonu: http://localhost:8000/docs

## Faz 1 akışı

```bash
# 1) Excel verisini yükle
make load-excel f=data/raw/sales.xlsx t=sales

# 2) Materialized view'ları oluştur (pre-aggregation)
psql "$DATABASE_URL" -f scripts/materialized_views.sql

# 3) Soru sor
curl -X POST localhost:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "Son 3 ayda EU bölgesindeki toplam ciro nedir?"}'
```

## Geliştirme

| Komut          | Açıklama                          |
|----------------|-----------------------------------|
| `make lint`    | ruff lint                         |
| `make format`  | otomatik biçimlendir              |
| `make type`    | mypy statik tip kontrolü          |
| `make test`    | pytest + coverage                 |
| `make migrate` | Alembic migration uygula          |

## Yol haritası (sonraki fazlar)

- [ ] **Yetkilendirme / çok kullanıcılı yapı** — `core/security.py`, satır
      seviyesi yetki, `api/deps.py` içinde `current_user`.
- [ ] **Standart rapor artifact'leri** — parametreli, dinamik güncellenen
      satış/stok/tedarik raporları.
- [ ] **RAG** — ürün dökümanları için (yalnızca açıklama, sayısal veri değil).
- [ ] **MCP server** — modülü dış araçlara MCP üzerinden açma.

## Teknoloji

FastAPI · PostgreSQL (AWS RDS) · Redis (ElastiCache) · Anthropic Claude ·
SQLAlchemy (async) · Alembic · pandas · sqlglot · structlog
