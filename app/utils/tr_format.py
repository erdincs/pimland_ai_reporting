"""Türkçe sayı/para/yüzde format yardımcıları — adL Premium Brief."""
from __future__ import annotations


def tl(value: float | int | None) -> str:
    """₺1.234.567 — binlik nokta, önde ₺."""
    if value is None:
        return "—"
    v = round(float(value))
    formatted = f"{abs(v):,}".replace(",", ".")
    sign = "−" if v < 0 else ""
    return f"{sign}₺{formatted}"


def pct(value: float | None, decimals: int = 1) -> str:
    """%14,7 — önde %, ondalık virgül."""
    if value is None:
        return "—"
    v = float(value)
    int_part = int(abs(v))
    frac = round(abs(v) - int_part, decimals)
    frac_str = str(round(frac * (10 ** decimals))).zfill(decimals)
    sign = "−" if v < 0 else ""
    return f"{sign}%{int_part},{frac_str}"


def num(value: float | int | None) -> str:
    """1.904 — binlik nokta, tam sayı."""
    if value is None:
        return "—"
    return f"{round(float(value)):,}".replace(",", ".")


def delta_class(value: float | None) -> str:
    """up / down / flat CSS sınıfı."""
    if value is None or abs(float(value)) < 0.05:
        return "flat"
    return "up" if float(value) > 0 else "down"


def delta_tri(value: float | None) -> str:
    """▴ / ▾ / ◆ trend oku."""
    cls = delta_class(value)
    return {"up": "▴", "down": "▾", "flat": "◆"}[cls]


def delta_html(value: float | None, show_sign: bool = True) -> str:
    """<span class='up tri tri-up'>+%2,4</span> hazır HTML."""
    if value is None:
        return "<span class='flat tri tri-flat'>—</span>"
    cls = delta_class(value)
    tri = {"up": "tri-up", "down": "tri-down", "flat": "tri-flat"}[cls]
    sign = "+" if value > 0 and show_sign else ("−" if value < 0 else "")
    val_str = pct(abs(value)) if show_sign else pct(value)
    return f"<span class='{cls} tri {tri}'>{sign}{val_str}</span>"
