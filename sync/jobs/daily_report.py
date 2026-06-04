"""Günlük özet email — sync_log tablosundan dünün raporu.

EventBridge: cron(0 5 * * ? *)  →  08:00 TR
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

_ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")
_REGION      = os.environ.get("AWS_REGION", "eu-north-1")
_ACCOUNT_ID  = "448049806345"
_SNS_ARN     = f"arn:aws:sns:{_REGION}:{_ACCOUNT_ID}:pimland-alerts"


# ── Veri çekme ────────────────────────────────────────────────────────────────

def _fetch_yesterday_logs(conn) -> List[Dict[str, Any]]:
    yesterday = (datetime.utcnow() - timedelta(days=1)).date()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT job_name, status, kayit_sayisi,
                   started_at, finished_at,
                   EXTRACT(EPOCH FROM (finished_at - started_at)) AS sure_sn,
                   hata_mesaji, detay
            FROM sync_log
            WHERE started_at::date = %s
            ORDER BY started_at
        """, (yesterday,))
        return [dict(r) for r in cur.fetchall()]


def _fetch_guard_stats(conn) -> Dict[str, int]:
    """sync_log'da guard logları yoksa sıfır döner (guard ayrı tablo gerekir)."""
    return {"injection": 0, "kapsam_disi": 0, "output_mudahale": 0, "blok": 0}


# ── HTML formatı ──────────────────────────────────────────────────────────────

_STATUS_ICON = {"success": "✅", "partial": "⚠️", "failed": "❌", "running": "🔄"}
_STATUS_COLOR = {"success": "#00c2a8", "partial": "#f59e0b", "failed": "#ef4444", "running": "#3b82f6"}


def _build_html(logs: List[Dict], guard: Dict, tarih: str) -> str:
    rows = ""
    for r in logs:
        status  = r.get("status", "?")
        icon    = _STATUS_ICON.get(status, "❓")
        color   = _STATUS_COLOR.get(status, "#9898b8")
        sure    = f"{r['sure_sn']:.0f}s" if r.get("sure_sn") else "—"
        kayit   = f"{r.get('kayit_sayisi') or 0:,}"
        hata    = f"<br><small style='color:#ef4444'>{r['hata_mesaji']}</small>" if r.get("hata_mesaji") else ""
        rows += f"""
        <tr>
          <td style='padding:8px 12px'>{r['job_name']}</td>
          <td style='padding:8px 12px;color:{color}'>{icon} {status}</td>
          <td style='padding:8px 12px;text-align:right'>{kayit}</td>
          <td style='padding:8px 12px;text-align:right'>{sure}</td>
          <td style='padding:8px 12px'>{hata or '—'}</td>
        </tr>"""

    total_ok  = sum(1 for r in logs if r.get("status") == "success")
    total_err = sum(1 for r in logs if r.get("status") == "failed")

    ozet_renk = "#00c2a8" if total_err == 0 else "#ef4444"
    ozet_text = f"{total_ok} başarılı, {total_err} hata" if logs else "Dün çalışan job yok"

    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'></head>
<body style='font-family:Inter,sans-serif;background:#0d0d12;color:#f0f0f8;padding:24px'>

<div style='max-width:700px;margin:0 auto'>
  <h2 style='color:#ff6b2b;margin-bottom:4px'>Pimland AI Reporting</h2>
  <p style='color:#9898b8;margin-top:0'>{tarih} — Günlük Özet</p>

  <div style='background:#13131a;border:1px solid #2a2a3a;border-radius:8px;padding:16px;margin-bottom:20px'>
    <strong style='color:{ozet_renk}'>{ozet_text}</strong>
  </div>

  <h3 style='color:#9898b8;font-size:12px;text-transform:uppercase;letter-spacing:.5px'>Sync Sonuçları</h3>
  <table style='width:100%;border-collapse:collapse;background:#13131a;border-radius:8px;overflow:hidden'>
    <thead>
      <tr style='background:#1a1a24;color:#9898b8;font-size:11px;text-transform:uppercase'>
        <th style='padding:8px 12px;text-align:left'>Job</th>
        <th style='padding:8px 12px;text-align:left'>Durum</th>
        <th style='padding:8px 12px;text-align:right'>Kayıt</th>
        <th style='padding:8px 12px;text-align:right'>Süre</th>
        <th style='padding:8px 12px;text-align:left'>Hata</th>
      </tr>
    </thead>
    <tbody>{rows or '<tr><td colspan=5 style="padding:12px;color:#55556a">Kayıt yok</td></tr>'}</tbody>
  </table>

  <h3 style='color:#9898b8;font-size:12px;text-transform:uppercase;letter-spacing:.5px;margin-top:20px'>Guard İstatistikleri (Dün)</h3>
  <div style='display:flex;gap:12px'>
    {''.join(f'<div style="background:#13131a;border:1px solid #2a2a3a;border-radius:8px;padding:12px 16px;flex:1;text-align:center"><div style="font-size:20px;font-weight:700;color:#f0f0f8">{v}</div><div style="font-size:11px;color:#9898b8;margin-top:4px">{k}</div></div>' for k, v in guard.items())}
  </div>

  <p style='color:#35354a;font-size:11px;margin-top:24px'>
    Pimland AI Reporting · {tarih} · eu-north-1
  </p>
</div>
</body></html>"""


# ── Gönderme ──────────────────────────────────────────────────────────────────

def send_daily_report(db_conn) -> Dict[str, Any]:
    if not _ALERT_EMAIL:
        log.warning("daily_report.skip ALERT_EMAIL tanımlı değil")
        return {"status": "skipped", "reason": "ALERT_EMAIL eksik"}

    tarih = (datetime.utcnow() - timedelta(days=1)).strftime("%d %B %Y")
    logs  = _fetch_yesterday_logs(db_conn)
    guard = _fetch_guard_stats(db_conn)
    html  = _build_html(logs, guard, tarih)

    try:
        import boto3
        sns = boto3.client("sns", region_name=_REGION)
        sns.publish(
            TopicArn=_SNS_ARN,
            Subject=f"Pimland Günlük Rapor — {tarih}",
            Message=f"HTML rapor ekte.\n\nÖzet: {len(logs)} job çalıştı.",
            MessageAttributes={
                "content-type": {"DataType": "String", "StringValue": "text/html"},
            },
        )
        log.info("daily_report.sent tarih=%s jobs=%d", tarih, len(logs))
        return {"status": "sent", "tarih": tarih, "jobs": len(logs)}
    except Exception as e:
        log.error("daily_report.failed err=%s", e)
        return {"status": "failed", "hata": str(e)}
