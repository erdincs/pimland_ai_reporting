"""colorCode → deterministik mock gradient (S-3 fallback).

colorCode hash'lenerek kahve/lacivert/gri ağırlıklı ton çiftine dönüştürülür.
Gerçek renk→hex eşlemesi geldiğinde bu modül değiştirilir.
"""
from __future__ import annotations

# (açık ton, koyu ton) çiftleri — premium dark palette
_PALETTES: list[tuple[str, str]] = [
    ("#7a5c3a", "#2c2218"),  # Bakır/kahve
    ("#3a4c5c", "#1c2630"),  # Lacivert
    ("#5c4a6a", "#2a1e30"),  # Mor/patlıcan
    ("#4a5c3a", "#1e2c18"),  # Haki
    ("#6a5040", "#2e1e16"),  # Toprak
    ("#3a5050", "#182020"),  # Koyu deniz
    ("#5c5040", "#28221a"),  # Camel
    ("#404060", "#1a1a2c"),  # Gece mavisi
]


def gradient_for(color_code: str) -> str:
    """linear-gradient(135deg, {açık}, {koyu}) — deterministic per colorCode."""
    idx = hash(str(color_code)) % len(_PALETTES)
    light, dark = _PALETTES[idx]
    return f"linear-gradient(135deg,{light},{dark})"
