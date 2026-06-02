# Pimland AI Reporting — Claude Code Rehberi

Bu proje Pimland PLM sistemi için FastAPI tabanlı AI raporlama modülüdür.

## Proje Skills

Bu projede iki özel skill tanımlıdır — **her zaman** bu skill'leri oku:

- **pimland-design-system** → Herhangi bir HTML/CSS/dashboard artifact üretirken
- **pimland-report-generator** → SQL sorgusu, veri analizi veya rapor pipeline'ı yazarken

## Temel Kurallar

1. **Agent ham veriyi ASLA görmez** — SQL üretir, DB hesaplar, sadece özet (≤200 satır) LLM'e döner
2. **incorta_satis ana kaynak** — `eticaret_satis` eski (Excel), yeni sorgular `incorta_satis` kullanmalı
3. **Materialized view önce** — `mv_satis_*` view'lar `incorta_satis`'ten daha hızlı
4. **Read-only DB** — tüm sorgular `pimland_ro` kullanıcısıyla `AsyncSession` üzerinde çalışır
5. **Türkçe yanıt** — kullanıcı Türkçe sorarsa Türkçe yanıt ver

## API Adresleri (Lokal)

- Portal: `http://localhost:8000/api/v1/portal`
- NL Sorgu: `POST http://localhost:8000/api/v1/query`
- Swagger: `http://localhost:8000/docs`

## Çalıştırma

```bash
source .venv/bin/activate
# veya
PYTHONPATH=. .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Önemli Dosyalar

- `app/agent/schema_context.py` — DB şeması (LLM'e gönderilen)
- `app/agent/prompts/text_to_sql.py` — SQL üretim prompt'u
- `app/services/sql_guard.py` — SQL güvenlik katmanı
- `app/reports/portal_queries.py` — Portal SQL sorguları
- `app/connectors/` — MCP/API veri bağlayıcıları
- `config/sources/` — Veri kaynağı YAML konfigürasyonları
