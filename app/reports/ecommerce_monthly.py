"""E-ticaret aylık satış raporu HTML üreticisi.

Tasarım dili: eticaret_demo_nisan2026_1.html ile aynı dark-theme, CSS variables,
KPI strip, donut charts, channel rankings, product cards.
"""

from __future__ import annotations

import math
from typing import List

from app.reports.data_queries import KanalRow, ProductRow, ReportData, TrendRow

# ── Renk paleti (CSS var'larına karşılık gelir) ──────────────────────────────
_KANAL_RENK = {
    "TRENDYOL":        "#f97316",   # orange
    "ADL":             "#00c2a8",   # teal
    "ADL IOS APP":     "#3b82f6",   # blue
    "ADL ANDROID APP": "#a855f7",   # purple
    "HEPSIBURADA":     "#ec4899",   # pink
    "BOYNER":          "#f59e0b",   # yellow
    "LOVEMYBODY":      "#22c55e",   # green
    "LMB IOS APP":     "#6b7280",   # gray
    "LMB ANDROID APP": "#14b8a6",   # teal2
    "TY ADL AZ":       "#ef4444",   # red
    "TY LMB AZ":       "#8b5cf6",   # violet
}

_AY_ADI = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}

_URUN_EMOJI = ["👗", "👕", "👖", "🧥", "🩺", "👚", "🧣", "👒", "👙", "🩳"]


def _fmt_m(val: float) -> str:
    """Format million TL: 91.72M ₺"""
    return f"{val:,.1f}M ₺".replace(",", ".")


def _fmt_k(val: float) -> str:
    if val >= 1_000_000:
        return f"{val/1_000_000:.2f}M ₺"
    if val >= 1_000:
        return f"{val/1_000:.0f}K ₺"
    return f"{val:,.0f} ₺"


def _fmt_adet(val: int) -> str:
    return f"{val:,}".replace(",", ".")


def _iade_renk(pct: float) -> str:
    if pct <= 15:
        return "var(--green)"
    if pct <= 25:
        return "var(--yellow)"
    return "var(--red)"


def _iade_tag(pct: float) -> str:
    if pct <= 15:
        return "<span style='color:var(--green)'>İyi</span>"
    if pct <= 25:
        return "<span style='color:var(--yellow)'>Orta</span>"
    return "<span style='color:var(--red)'>Yüksek</span>"


def _sparkline_points(values: list, w: int = 80, h: int = 28) -> str:
    if not values:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn or 1
    pts = []
    for i, v in enumerate(values):
        x = round(i / max(len(values) - 1, 1) * w, 1)
        y = round(h - (v - mn) / rng * (h - 4) - 2, 1)
        pts.append(f"{x},{y}")
    return " ".join(pts)


def _donut_segments(items: list, colors: list) -> str:
    """Generate SVG circle segments for donut chart."""
    r = 36
    circ = 2 * math.pi * r  # ≈ 226.2
    cx, cy = 45, 45
    offset = 0.0
    segs = []
    for (label, pct), color in zip(items, colors):
        arc = pct / 100 * circ
        segs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="14" stroke-dasharray="{arc:.1f} {circ:.1f}" '
            f'stroke-dashoffset="{-offset:.1f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += arc
    return "\n".join(segs)


def _trend_polyline(trend: List[TrendRow], key: str, w: int = 600, h: int = 180) -> str:
    vals = [getattr(t, key) for t in trend]
    if not vals:
        return ""
    mx = max(vals) or 1
    pts = []
    for i, v in enumerate(vals):
        x = round(i / max(len(vals) - 1, 1) * w, 1)
        y = round(h - 10 - (v / mx) * (h - 20), 1)
        pts.append(f"{x},{y}")
    return " ".join(pts)


# ── Ana HTML üretici ────────────────────────────────────────────────────────

def render(data: ReportData) -> str:
    d = data
    k = d.kpis
    ay_adi = _AY_ADI.get(d.ay, str(d.ay))

    # Trend sparkline points (aylık brüt)
    spark_brut = _sparkline_points([t.brut_m for t in d.trend])
    spark_iade = _sparkline_points([t.iade_m for t in d.trend])

    # Trend chart polylines
    pl_brut = _trend_polyline(d.trend, "brut_m")
    pl_iade = _trend_polyline(d.trend, "iade_m")
    pl_net  = _trend_polyline(d.trend, "net_m")

    # Kanal satış donut items
    kanal_donut_items = [(r.kanal, r.pay) for r in d.kanal_satis[:6]]
    kanal_donut_colors = [_KANAL_RENK.get(r.kanal, "#6b7280") for r in d.kanal_satis[:6]]

    # Iade oranı rengi
    iade_renk = _iade_renk(k.iade_oran)

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>E-Ticaret Satış Analizi · {ay_adi} {d.yil}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --bg:#0d0d12;--bg2:#13131a;--bg3:#1a1a24;--bg4:#22222f;
  --border:#2a2a3a;--border2:#333348;
  --t1:#f0f0f8;--t2:#9898b8;--t3:#55556a;--t4:#35354a;
  --orange:#ff6b2b;--teal:#00c2a8;--green:#22c55e;
  --red:#ef4444;--yellow:#f59e0b;--blue:#3b82f6;--purple:#a855f7;
}}
body{{background:var(--bg);color:var(--t1);font-family:'Inter',system-ui,sans-serif;font-size:13px;line-height:1.5;min-height:100vh}}
.topbar{{background:var(--bg2);border-bottom:1px solid var(--border);padding:0 32px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}}
.topbar-brand{{display:flex;align-items:center;gap:10px}}
.dot{{width:8px;height:8px;background:var(--orange);border-radius:50%}}
.topbar-brand span{{font-size:14px;font-weight:600;letter-spacing:-.3px}}
.topbar-brand small{{font-size:11px;color:var(--t3)}}
.filters{{display:flex;align-items:center;gap:8px}}
.filter-chip{{background:var(--bg3);border:1px solid var(--border);color:var(--t2);font-size:11px;font-weight:500;padding:5px 12px;border-radius:6px;white-space:nowrap}}
.filter-chip.month{{border-color:var(--orange);color:var(--orange)}}
.topbar-right{{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--t3)}}
.live-dot{{width:6px;height:6px;background:var(--green);border-radius:50%;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.page{{padding:28px 32px;max-width:1600px;margin:0 auto;display:flex;flex-direction:column;gap:28px}}
.sec-hdr{{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:16px}}
.sec-title{{font-size:12px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:var(--t2)}}
.sec-sub{{font-size:11px;color:var(--t3)}}
/* Executive Summary */
.exec-summary{{background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:4px}}
.exec-top{{display:grid;grid-template-columns:1fr 1fr 1fr;border-bottom:1px solid var(--border)}}
.exec-kpi-big{{padding:28px 28px 22px;border-right:1px solid var(--border)}}
.exec-kpi-big:last-child{{border-right:none}}
.tag{{font-size:9px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;display:inline-block;padding:3px 8px;border-radius:3px}}
.tag-orange{{background:rgba(255,107,43,.12);color:var(--orange)}}
.tag-teal{{background:rgba(0,194,168,.1);color:var(--teal)}}
.tag-red{{background:rgba(239,68,68,.1);color:var(--red)}}
.big-num{{font-size:38px;font-weight:700;letter-spacing:-1.5px;line-height:1;margin-bottom:6px}}
.big-label{{font-size:11px;color:var(--t3)}}
.sub-row{{display:flex;gap:16px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}}
.sub-item{{display:flex;flex-direction:column;gap:2px}}
.s-val{{font-size:14px;font-weight:600;color:var(--t1)}}
.s-lbl{{font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px}}
.exec-bottom{{display:grid;grid-template-columns:1fr 1fr 1fr}}
.exec-col{{padding:22px 28px;border-right:1px solid var(--border)}}
.exec-col:last-child{{border-right:none}}
.exec-col-title{{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--t3);margin-bottom:14px}}
.alert-list{{display:flex;flex-direction:column;gap:8px}}
.alert-item{{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border-radius:6px;border:1px solid transparent}}
.alert-item.crit{{background:rgba(239,68,68,.06);border-color:rgba(239,68,68,.2)}}
.alert-item.warn{{background:rgba(245,158,11,.05);border-color:rgba(245,158,11,.15)}}
.alert-item.good{{background:rgba(34,197,94,.05);border-color:rgba(34,197,94,.15)}}
.alert-icon{{font-size:14px;flex-shrink:0;margin-top:1px}}
.alert-title{{font-size:11px;font-weight:600;color:var(--t1);margin-bottom:2px}}
.alert-desc{{font-size:10px;color:var(--t2);line-height:1.4}}
.action-list{{display:flex;flex-direction:column;gap:8px}}
.action-item{{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;background:var(--bg3);border-radius:6px;border:1px solid var(--border)}}
.action-num{{width:20px;height:20px;border-radius:4px;background:var(--orange);color:#fff;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}}
.action-title{{font-size:11px;font-weight:600;color:var(--t1);margin-bottom:2px}}
.action-desc{{font-size:10px;color:var(--t3);line-height:1.4}}
/* KPI Strip */
.kpi-strip{{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--border);border-radius:10px;overflow:hidden;border:1px solid var(--border)}}
.kpi-card{{background:var(--bg2);padding:18px 16px 14px;cursor:pointer;transition:background .15s;position:relative;overflow:hidden}}
.kpi-card:hover{{background:var(--bg3)}}
.kpi-label{{font-size:10px;font-weight:500;letter-spacing:.5px;text-transform:uppercase;color:var(--t3);margin-bottom:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.kpi-val{{font-size:20px;font-weight:700;color:var(--t1);line-height:1;letter-spacing:-.5px;margin-bottom:8px}}
.kpi-val.orange{{color:var(--orange)}}
.kpi-val.teal{{color:var(--teal)}}
.kpi-val.red{{color:var(--red)}}
.kpi-val.green{{color:var(--green)}}
.kpi-change{{font-size:10px;font-weight:600}}
.kpi-change.down{{color:var(--red)}}
.kpi-change.neutral{{color:var(--t3)}}
.sparkline svg{{display:block;margin-top:6px}}
/* Trend chart */
.chart-card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:20px}}
.chart-title{{font-size:12px;font-weight:600;color:var(--t1);margin-bottom:4px}}
.chart-legend{{display:flex;gap:16px;margin-bottom:12px}}
.legend-item{{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)}}
.legend-dot{{width:8px;height:3px;border-radius:2px}}
/* Rankings */
.ranking-card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
.ranking-header{{padding:14px 16px;border-bottom:1px solid var(--border);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--t2)}}
.ranking-list{{overflow-y:auto}}
.ranking-row{{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid rgba(42,42,58,.3);cursor:pointer;transition:background .12s}}
.ranking-row:hover{{background:var(--bg3)}}
.ranking-row:last-child{{border-bottom:none}}
.rank-badge{{width:22px;height:22px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}}
.rb1{{background:rgba(255,107,43,.15);color:var(--orange)}}
.rb2{{background:rgba(0,194,168,.1);color:var(--teal)}}
.rb3{{background:rgba(59,130,246,.1);color:var(--blue)}}
.rbn{{background:var(--bg4);color:var(--t3)}}
.r-name{{flex:1;font-size:12px;color:var(--t1);font-weight:500}}
.r-kpi{{display:flex;flex-direction:column;align-items:flex-end;gap:2px}}
.r-val{{font-size:11px;font-weight:600;color:var(--t1)}}
.r-sub{{font-size:10px;color:var(--t3)}}
/* Donut */
.donut-card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:18px}}
.donut-card-title{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--t2);margin-bottom:14px}}
.donut-wrap{{display:flex;align-items:center;gap:12px}}
.donut-legend{{flex:1;display:flex;flex-direction:column;gap:5px}}
.dl-row{{display:flex;align-items:center;justify-content:space-between;gap:6px}}
.dl-dot{{width:8px;height:8px;border-radius:2px;flex-shrink:0}}
.dl-name{{font-size:10px;color:var(--t2);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.dl-pct{{font-size:10px;font-weight:600;color:var(--t1)}}
/* Product cards */
.product-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}}
.product-card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden;cursor:pointer;transition:all .2s}}
.product-card:hover{{border-color:var(--border2);transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.3)}}
.product-img{{height:110px;background:var(--bg3);display:flex;align-items:center;justify-content:center;position:relative}}
.product-img-placeholder{{font-size:28px;opacity:.35}}
.product-rank-badge{{position:absolute;top:8px;left:8px;background:var(--orange);color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px}}
.product-body{{padding:12px}}
.product-name{{font-size:11px;font-weight:600;color:var(--t1);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.product-cat{{font-size:10px;color:var(--t3);margin-bottom:10px;font-family:monospace}}
.product-stat-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}}
.ps-label{{font-size:10px;color:var(--t3)}}
.ps-val{{font-size:10px;font-weight:600;color:var(--t1)}}
.hbar-track{{height:4px;background:var(--bg4);border-radius:2px;margin-top:8px;overflow:hidden}}
.hbar-fill{{height:100%;border-radius:2px;transition:width 1.2s cubic-bezier(.16,1,.3,1)}}
.iade-label{{display:flex;justify-content:space-between;margin-bottom:2px;font-size:9px;color:var(--t3)}}
/* Horizontal bar */
.hbar-card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:18px}}
.hbar-title{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--t2);margin-bottom:14px}}
.hbar-list{{display:flex;flex-direction:column;gap:9px}}
.hbar-row{{display:flex;flex-direction:column;gap:4px}}
.hbar-meta{{display:flex;justify-content:space-between;align-items:baseline}}
.hbar-name{{font-size:11px;color:var(--t2)}}
.hbar-val{{font-size:10px;font-weight:600;color:var(--t1)}}
/* Footer */
.report-footer{{padding:16px 0;border-top:1px solid var(--border);display:flex;justify-content:space-between;font-size:10px;color:var(--t3)}}
/* Animations */
@keyframes fadeUp{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}
.anim{{animation:fadeUp .45s ease both}}
.anim-d1{{animation-delay:.05s}}.anim-d2{{animation-delay:.1s}}.anim-d3{{animation-delay:.15s}}
.anim-d4{{animation-delay:.2s}}.anim-d5{{animation-delay:.25s}}
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <div class="topbar-brand">
    <div class="dot"></div>
    <div>
      <span>E-Ticaret Satış Analizi</span>
      <small style="margin-left:10px">{ay_adi} {d.yil} · Incorta Live Data</small>
    </div>
  </div>
  <div class="filters">
    <div class="filter-chip month">{ay_adi} {d.yil}</div>
    <div class="filter-chip">Tüm Kanallar</div>
    <div class="filter-chip">Tüm Ürünler</div>
  </div>
  <div class="topbar-right">
    <div class="live-dot"></div>
    incorta_satis · incorta_depo_iade · incorta_iptal_siparis
  </div>
</div>

<div class="page">

<!-- ══ YÖNETİCİ ÖZETİ ══ -->
<div class="exec-summary anim">
  <div class="exec-top">
    <div class="exec-kpi-big">
      <span class="tag tag-orange">Brüt Ciro</span>
      <div class="big-num" style="color:var(--orange)">{_fmt_m(k.brut_ciro/1_000_000)}</div>
      <div class="big-label">{ay_adi} {d.yil} toplam brüt satış</div>
      <div class="sub-row">
        <div class="sub-item"><div class="s-val">{_fmt_adet(k.brut_adet)}</div><div class="s-lbl">Brüt Adet</div></div>
        <div class="sub-item"><div class="s-val">{_fmt_k(k.brut_ciro/max(k.brut_adet,1))}</div><div class="s-lbl">Brüt OBF</div></div>
      </div>
    </div>
    <div class="exec-kpi-big">
      <span class="tag tag-teal">Net Ciro</span>
      <div class="big-num" style="color:var(--teal)">{_fmt_m(k.net_ciro/1_000_000)}</div>
      <div class="big-label">İade & iptal düşülmüş gerçekleşen</div>
      <div class="sub-row">
        <div class="sub-item"><div class="s-val">{_fmt_adet(k.net_adet)}</div><div class="s-lbl">Net Adet</div></div>
        <div class="sub-item"><div class="s-val">{_fmt_k(k.net_obf)}</div><div class="s-lbl">Net OBF</div></div>
        <div class="sub-item"><div class="s-val">{_fmt_m(k.iptal_ciro/1_000_000)}</div><div class="s-lbl">İptal Ciro</div></div>
      </div>
    </div>
    <div class="exec-kpi-big">
      <span class="tag tag-red">İade Ciro</span>
      <div class="big-num" style="color:var(--red)">{_fmt_m(k.iade_ciro/1_000_000)}</div>
      <div class="big-label">Toplam iade — net ciro erimesi</div>
      <div class="sub-row">
        <div class="sub-item"><div class="s-val" style="color:var(--red)">%{k.iade_oran}</div><div class="s-lbl">İade Oranı</div></div>
        <div class="sub-item"><div class="s-val">{_fmt_m((k.iade_ciro+k.iptal_ciro)/1_000_000)}</div><div class="s-lbl">Toplam Kayıp</div></div>
      </div>
    </div>
  </div>

  <div class="exec-bottom">
    <div class="exec-col">
      <div class="exec-col-title">⚠ Dikkat Noktaları</div>
      <div class="alert-list">
        {"".join(_alert_items(data))}
      </div>
    </div>
    <div class="exec-col">
      <div class="exec-col-title">✦ Kanal Satış Dağılımı</div>
      {"".join(_kanal_perf_table(d.kanal_satis))}
    </div>
    <div class="exec-col">
      <div class="exec-col-title">◈ Acil Aksiyon</div>
      {"".join(_aksiyon_listesi(data))}
    </div>
  </div>
</div>

<!-- ══ 01 · KPI STRIP & TREND ══ -->
<div>
  <div class="sec-hdr">
    <span class="sec-title">01 · Executive Sales</span>
    <span class="sec-sub">Brüt satış + iade · {ay_adi} {d.yil}</span>
  </div>

  <div class="kpi-strip anim">
    <div class="kpi-card">
      <div class="kpi-label">Brüt Ciro</div>
      <div class="kpi-val orange">{_fmt_m(k.brut_ciro/1_000_000)}</div>
      <div class="kpi-change neutral">{ay_adi} {d.yil}</div>
      <svg width="100%" height="28" viewBox="0 0 80 28">
        <polyline fill="none" stroke="var(--orange)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" points="{spark_brut}"/>
      </svg>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Net Ciro</div>
      <div class="kpi-val teal">{_fmt_m(k.net_ciro/1_000_000)}</div>
      <div class="kpi-change neutral">İade & iptal düşülmüş</div>
      <svg width="100%" height="28" viewBox="0 0 80 28">
        <polyline fill="none" stroke="var(--teal)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" points="{spark_brut}"/>
      </svg>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">İade Ciro</div>
      <div class="kpi-val red">{_fmt_m(k.iade_ciro/1_000_000)}</div>
      <div class="kpi-change down">↑ %{k.iade_oran} iade oranı</div>
      <svg width="100%" height="28" viewBox="0 0 80 28">
        <polyline fill="none" stroke="var(--red)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" points="{spark_iade}"/>
      </svg>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Brüt Adet</div>
      <div class="kpi-val">{_fmt_adet(k.brut_adet)}</div>
      <div class="kpi-change neutral">Toplam kalem</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Net Adet</div>
      <div class="kpi-val teal">{_fmt_adet(k.net_adet)}</div>
      <div class="kpi-change neutral">İade düşülmüş</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Net OBF</div>
      <div class="kpi-val">{_fmt_k(k.net_obf)}</div>
      <div class="kpi-change neutral">Net ciro / net adet</div>
    </div>
  </div>

  <!-- Trend Chart -->
  <div class="chart-card anim anim-d1" style="margin-top:16px">
    <div class="chart-title">Aylık Ciro Trendi — {d.yil}</div>
    <div class="chart-legend">
      <div class="legend-item"><div class="legend-dot" style="background:var(--orange)"></div>Brüt Ciro</div>
      <div class="legend-item"><div class="legend-dot" style="background:var(--teal)"></div>Net Ciro</div>
      <div class="legend-item"><div class="legend-dot" style="background:var(--red)"></div>İade</div>
    </div>
    <div style="position:relative;height:180px">
      <svg width="100%" height="180" viewBox="0 0 600 180" preserveAspectRatio="none">
        <line x1="0" y1="45" x2="600" y2="45" stroke="var(--border)" stroke-width="1"/>
        <line x1="0" y1="90" x2="600" y2="90" stroke="var(--border)" stroke-width="1"/>
        <line x1="0" y1="135" x2="600" y2="135" stroke="var(--border)" stroke-width="1"/>
        <polyline fill="none" stroke="var(--orange)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" points="{pl_brut}"/>
        <polyline fill="none" stroke="var(--teal)"   stroke-width="2"   stroke-linecap="round" stroke-linejoin="round" points="{pl_net}"/>
        <polyline fill="none" stroke="var(--red)"    stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="4 3" points="{pl_iade}"/>
        {"".join(_trend_labels(d.trend))}
      </svg>
    </div>
  </div>
</div>

<!-- ══ 02 · KANAL PERFORMANS ══ -->
<div>
  <div class="sec-hdr">
    <span class="sec-title">02 · Kanal Performansı</span>
    <span class="sec-sub">Satış vs iade dağılımı · {ay_adi} {d.yil}</span>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 280px;gap:16px">

    <!-- Kanal satış -->
    <div class="ranking-card anim anim-d1">
      <div class="ranking-header">Kanal Bazlı Satış — Ciro Sıralaması</div>
      <div class="ranking-list">
        {"".join(_kanal_ranking_rows(d.kanal_satis))}
      </div>
    </div>

    <!-- Kanal iade -->
    <div class="ranking-card anim anim-d2">
      <div class="ranking-header">Kanal Bazlı İade — Tutar Sıralaması</div>
      <div class="ranking-list">
        {"".join(_kanal_iade_rows(d.kanal_iade))}
      </div>
    </div>

    <!-- Donut -->
    <div class="donut-card anim anim-d3">
      <div class="donut-card-title">Kanal Ciro Dağılımı</div>
      <div class="donut-wrap">
        <svg width="90" height="90" viewBox="0 0 90 90">
          <circle cx="45" cy="45" r="36" fill="none" stroke="var(--bg4)" stroke-width="14"/>
          {_donut_segments(kanal_donut_items, kanal_donut_colors)}
          <text x="45" y="49" text-anchor="middle" fill="var(--t1)" font-size="9" font-weight="700">{d.kanal_satis[0].kanal[:7] if d.kanal_satis else ""}</text>
        </svg>
        <div class="donut-legend">
          {"".join(_donut_legend_rows(d.kanal_satis[:6], kanal_donut_colors))}
        </div>
      </div>
    </div>

  </div>
</div>

<!-- ══ 03 · TOP 10 ÜRÜN ══ -->
<div>
  <div class="sec-hdr">
    <span class="sec-title">03 · Top Ürünler</span>
    <span class="sec-sub">Net ciro bazlı · {ay_adi} {d.yil}</span>
  </div>
  <div class="product-grid">
    {"".join(_product_cards(d.top_urunler[:5]))}
  </div>
  {"_top_urun_table(d.top_urunler)"}
</div>

<!-- FOOTER -->
<div class="report-footer">
  <span>E-Ticaret · {ay_adi} {d.yil} · Pimland AI Reporting</span>
  <span>Kaynak: Incorta Live Data · incorta_satis + incorta_depo_iade + incorta_iptal_siparis</span>
</div>

</div>

<script>
document.addEventListener('DOMContentLoaded',()=>{{
  document.querySelectorAll('.hbar-fill').forEach(el=>{{
    const w=el.style.width;el.style.width='0';
    setTimeout(()=>{{ el.style.width=w; }},400);
  }});
}});
</script>
</body>
</html>"""

    # Arka planda çağrılan yardımcı fonksiyonların template'e gömülmesi
    # (f-string içinde fonksiyon çağrısı yapılır)
    return html


# ── Yardımcı render fonksiyonları ────────────────────────────────────────────

def _alert_items(data: ReportData) -> list:
    k = data.kpis
    items = []
    if k.iade_oran > 30:
        items.append(f"""<div class="alert-item crit"><div class="alert-icon">🔴</div><div>
          <div class="alert-title">İade Oranı Kritik — %{k.iade_oran}</div>
          <div class="alert-desc">{_fmt_m(k.iade_ciro/1_000_000)} brüt ciro eriyor. Her {round(100/k.iade_oran,1)} satıştan 1'i iade.</div>
        </div></div>""")
    elif k.iade_oran > 20:
        items.append(f"""<div class="alert-item warn"><div class="alert-icon">🟡</div><div>
          <div class="alert-title">İade Oranı Yüksek — %{k.iade_oran}</div>
          <div class="alert-desc">{_fmt_m(k.iade_ciro/1_000_000)} ciro erimesi. Takip gerekiyor.</div>
        </div></div>""")
    if data.kanal_satis and data.kanal_iade:
        top_satis_pay = data.kanal_satis[0].pay
        top_iade_pay = data.kanal_iade[0].pay
        if top_iade_pay > top_satis_pay + 8:
            items.append(f"""<div class="alert-item warn"><div class="alert-icon">🟡</div><div>
              <div class="alert-title">{data.kanal_iade[0].kanal} İade Konsantrasyonu</div>
              <div class="alert-desc">Satış payı %{top_satis_pay} — iade payı %{top_iade_pay}. Dengesiz kanal riski.</div>
            </div></div>""")
    if data.top_urunler:
        yuksek_iade = [u for u in data.top_urunler if u.iade_pct > 40]
        if yuksek_iade:
            items.append(f"""<div class="alert-item crit"><div class="alert-icon">🔴</div><div>
              <div class="alert-title">Yüksek İadeli Ürünler — {len(yuksek_iade)} SKU</div>
              <div class="alert-desc">Top ürünlerde %40+ iade: {", ".join(u.urun_adi[:15] for u in yuksek_iade[:2])}...</div>
            </div></div>""")
    if not items:
        items.append("""<div class="alert-item good"><div class="alert-icon">🟢</div><div>
          <div class="alert-title">Performans Normal Seviyede</div>
          <div class="alert-desc">Kritik bir uyarı tespit edilmedi.</div>
        </div></div>""")
    return items


def _kanal_perf_table(rows: list) -> list:
    lines = ['<table style="width:100%;border-collapse:collapse">']
    for r in rows[:5]:
        color = _KANAL_RENK.get(r.kanal, "#6b7280")
        lines.append(f"""<tr style="border-bottom:1px solid rgba(42,42,58,.4)">
          <td style="padding:6px 0;font-size:11px;color:var(--t2)">
            <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{color};margin-right:7px"></span>
            {r.kanal}
          </td>
          <td style="padding:6px 0;font-size:11px;font-weight:600;color:var(--t1);text-align:right">
            {_fmt_m(r.ciro/1_000_000)} · %{r.pay}
          </td>
        </tr>""")
    lines.append("</table>")
    return lines


def _aksiyon_listesi(data: ReportData) -> list:
    k = data.kpis
    items = []
    if k.iade_oran > 25:
        items.append(f"""<div class="action-item">
          <div class="action-num">1</div>
          <div>
            <div class="action-title">İade Analizi Derinleştir</div>
            <div class="action-desc">%{k.iade_oran} iade oranı · {_fmt_m(k.iade_ciro/1_000_000)} kayıp</div>
          </div>
        </div>""")
    if data.kanal_iade:
        items.append(f"""<div class="action-item">
          <div class="action-num">2</div>
          <div>
            <div class="action-title">{data.kanal_iade[0].kanal} Kanal İncelemesi</div>
            <div class="action-desc">%{data.kanal_iade[0].pay} iade konsantrasyonu</div>
          </div>
        </div>""")
    if data.top_urunler:
        worst = max(data.top_urunler, key=lambda u: u.iade_pct)
        items.append(f"""<div class="action-item">
          <div class="action-num">3</div>
          <div>
            <div class="action-title">SKU İncelemesi: {worst.urun_adi[:20]}</div>
            <div class="action-desc">%{worst.iade_pct} iade · {worst.urun_kodu}</div>
          </div>
        </div>""")
    return items


def _kanal_ranking_rows(rows: list) -> list:
    badges = ["rb1", "rb2", "rb3"] + ["rbn"] * 10
    lines = []
    for i, r in enumerate(rows):
        lines.append(f"""<div class="ranking-row">
          <div class="rank-badge {badges[i]}">{i+1}</div>
          <div class="r-name">{r.kanal}</div>
          <div class="r-kpi">
            <div class="r-val">{_fmt_m(r.ciro/1_000_000)}</div>
            <div class="r-sub">{_fmt_adet(r.adet)} adet · %{r.pay}</div>
          </div>
        </div>""")
    return lines


def _kanal_iade_rows(rows: list) -> list:
    badges = ["rb1", "rb2", "rb3"] + ["rbn"] * 10
    lines = []
    for i, r in enumerate(rows):
        lines.append(f"""<div class="ranking-row">
          <div class="rank-badge {badges[i]}">{i+1}</div>
          <div class="r-name">{r.kanal}</div>
          <div class="r-kpi">
            <div class="r-val" style="color:var(--red)">{_fmt_m(r.ciro/1_000_000)}</div>
            <div class="r-sub">{_fmt_adet(r.adet)} adet · %{r.pay}</div>
          </div>
        </div>""")
    return lines


def _donut_legend_rows(rows: list, colors: list) -> list:
    return [
        f"""<div class="dl-row">
          <div class="dl-dot" style="background:{colors[i]}"></div>
          <div class="dl-name">{r.kanal}</div>
          <div class="dl-pct">%{r.pay}</div>
        </div>"""
        for i, r in enumerate(rows)
    ]


def _product_cards(products: list) -> list:
    cards = []
    for p in products:
        emoji = _URUN_EMOJI[p.rank - 1]
        iade_color = _iade_renk(p.iade_pct)
        iade_w = min(int(p.iade_pct), 100)
        cards.append(f"""<div class="product-card anim anim-d{p.rank}">
          <div class="product-img" style="background:linear-gradient(135deg,#1a1a24,#22222f)">
            <span class="product-img-placeholder">{emoji}</span>
            <div class="product-rank-badge">#{p.rank}</div>
          </div>
          <div class="product-body">
            <div class="product-name">{p.urun_adi}</div>
            <div class="product-cat">{p.urun_kodu}</div>
            <div class="product-stat-row"><span class="ps-label">Net Ciro</span><span class="ps-val" style="color:var(--teal)">{_fmt_k(p.net_ciro)}</span></div>
            <div class="product-stat-row"><span class="ps-label">Brüt Ciro</span><span class="ps-val">{_fmt_k(p.brut_ciro)}</span></div>
            <div class="product-stat-row"><span class="ps-label">Brüt Adet</span><span class="ps-val">{p.brut_adet}</span></div>
            <div class="iade-label"><span>İade %{p.iade_pct}</span>{_iade_tag(p.iade_pct)}</div>
            <div class="hbar-track"><div class="hbar-fill" style="width:{iade_w}%;background:{iade_color}"></div></div>
          </div>
        </div>""")
    return cards


def _top_urun_table(products: list) -> str:
    if len(products) <= 5:
        return ""
    rows = ""
    for p in products[5:]:
        iade_color = _iade_renk(p.iade_pct)
        rows += f"""<tr style="border-bottom:1px solid rgba(42,42,58,.3)">
          <td style="padding:9px 14px;font-size:12px;font-weight:500;color:var(--t1)">{p.rank}. {p.urun_adi}</td>
          <td style="padding:9px 14px;font-size:11px;color:var(--t3);font-family:monospace">{p.urun_kodu}</td>
          <td style="padding:9px 14px;font-size:11px;color:var(--teal);text-align:right">{_fmt_k(p.net_ciro)}</td>
          <td style="padding:9px 14px;font-size:11px;text-align:right">{_fmt_k(p.brut_ciro)}</td>
          <td style="padding:9px 14px;font-size:11px;text-align:right">{p.brut_adet}</td>
          <td style="padding:9px 14px;font-size:11px;text-align:right;color:{iade_color}">%{p.iade_pct}</td>
        </tr>"""
    return f"""<div style="margin-top:16px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;overflow:hidden">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--border)">
          <th style="padding:12px 14px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--t3);text-align:left">Ürün</th>
          <th style="padding:12px 14px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--t3);text-align:left">SKU</th>
          <th style="padding:12px 14px;font-size:10px;font-weight:600;text-transform:uppercase;color:var(--t3);text-align:right">Net Ciro</th>
          <th style="padding:12px 14px;font-size:10px;font-weight:600;text-transform:uppercase;color:var(--t3);text-align:right">Brüt Ciro</th>
          <th style="padding:12px 14px;font-size:10px;font-weight:600;text-transform:uppercase;color:var(--t3);text-align:right">Adet</th>
          <th style="padding:12px 14px;font-size:10px;font-weight:600;text-transform:uppercase;color:var(--t3);text-align:right">İade %</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _trend_labels(trend: List[TrendRow]) -> list:
    labels = []
    ay_names = {1:"Oca",2:"Şub",3:"Mar",4:"Nis",5:"May",6:"Haz",7:"Tem",8:"Ağu",9:"Eyl",10:"Eki",11:"Kas",12:"Ara"}
    n = len(trend)
    for i, t in enumerate(trend):
        x = round(i / max(n - 1, 1) * 600, 1)
        labels.append(f'<text x="{x}" y="178" fill="var(--t3)" font-size="9" text-anchor="middle">{ay_names.get(t.ay,"")}</text>')
    return labels
