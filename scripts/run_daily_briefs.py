"""Tüm aktif günlük zamanlamalar için brief üretir."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from app.core.config import settings
from app.services.daily_brief.orchestrator import generate_brief


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    today = date.today()
    print(f"Brief üretim başlıyor — tarih: {today}\n")

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT s.id, s.name, s.profile_id, s.tone, p.name AS profile_name
            FROM brief_schedules s
            JOIN brief_profiles p ON p.id = s.profile_id
            WHERE s.frequency_type = 'daily'
              AND s.is_active = true
              AND p.is_active = true
              AND (
                  SELECT COUNT(*) FROM brief_questions q
                  WHERE q.schedule_id = s.id AND q.is_active = true
              ) > 0
            ORDER BY s.id
        """))).mappings().all()

        schedules = [dict(r) for r in rows]

    print(f"Aktif günlük zamanlama: {len(schedules)} adet\n")

    for s in schedules:
        print(f"  [{s['id']}] {s['profile_name']} / {s['name']}")
        async with AsyncSessionLocal() as session:
            try:
                result = await generate_brief(s["id"], session, target_date=today)
                if "hata" in result:
                    print(f"    ✗ HATA: {result['hata']}")
                else:
                    si = result.get("brief", {}).get("top_insights", [])
                    print(f"    ✓ {result['soru_sayisi']} soru, {result['generation_ms']}ms, "
                          f"{len(si)} insight")
            except Exception as exc:
                print(f"    ✗ EXCEPTION: {exc}")

    await engine.dispose()
    print("\nTamamlandı.")


if __name__ == "__main__":
    asyncio.run(main())
