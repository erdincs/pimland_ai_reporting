"""adL Premium Brief v2 — utils birim testleri."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from app.utils.tr_format import tl, pct, num, delta_class, delta_tri, delta_html
from app.utils.adl_url import slugify, urun_url, urun_thumb_url, CDN_BASE, SITE_BASE
from app.utils.mock_color import gradient_for


# ── tr_format ────────────────────────────────────────────────────────────────

class TestTl:
    def test_pozitif(self):
        assert tl(1_234_567) == "₺1.234.567"

    def test_sifir(self):
        assert tl(0) == "₺0"

    def test_negatif(self):
        assert tl(-50_000) == "−₺50.000"

    def test_none(self):
        assert tl(None) == "—"

    def test_float_round(self):
        assert tl(1_234.6) == "₺1.235"


class TestPct:
    def test_normal(self):
        assert pct(14.7) == "%14,7"

    def test_sifir_ondalik(self):
        assert pct(8.0) == "%8,0"

    def test_negatif(self):
        assert pct(-3.2) == "−%3,2"

    def test_none(self):
        assert pct(None) == "—"


class TestNum:
    def test_binlik(self):
        assert num(1_904) == "1.904"

    def test_kucuk(self):
        assert num(42) == "42"

    def test_none(self):
        assert num(None) == "—"


class TestDelta:
    def test_up(self):
        assert delta_class(5.2) == "up"
        assert delta_tri(5.2) == "▴"

    def test_down(self):
        assert delta_class(-3.1) == "down"
        assert delta_tri(-3.1) == "▾"

    def test_flat(self):
        assert delta_class(0.0) == "flat"
        assert delta_tri(0.0) == "◆"

    def test_delta_html_up(self):
        html = delta_html(2.4)
        assert "up" in html
        assert "tri-up" in html
        assert "▴" not in html  # ▴ CSS pseudo-element'ten gelir

    def test_delta_html_none(self):
        html = delta_html(None)
        assert "flat" in html

    def test_no_prohibited_arrows(self):
        for v in [5.0, -5.0, 0.0]:
            html = delta_html(v)
            assert "▲" not in html
            assert "▼" not in html


# ── adl_url ──────────────────────────────────────────────────────────────────

class TestSlugify:
    def test_turkce(self):
        assert slugify("Saten Midi Elbise") == "saten-midi-elbise"

    def test_ozel_karakter(self):
        assert slugify("Güneş Işığı") == "gunes-isigi"

    def test_coklu_bosluk(self):
        assert slugify("  Keten  Pantolon  ") == "keten-pantolon"


class TestUrunUrl:
    def test_slug_gecildi(self):
        url = urun_url("elbise", "Saten Midi Elbise", "12345", "620")
        assert url == f"{SITE_BASE}/elbise/saten-midi-elbise-p-12345-620"

    def test_bos_slug_fallback(self):
        url = urun_url("", "Test Ürün", "99999", "100")
        assert "/urun/" in url

    def test_none_slug_fallback(self):
        url = urun_url(None, "Test Ürün", "99999", "100")
        assert "/urun/" in url

    def test_turkce_adi(self):
        url = urun_url("triko", "Örgü Triko", "55555", "410")
        assert "orgu-triko" in url
        assert "/triko/" in url


class TestUrunThumbUrl:
    def test_format(self):
        url = urun_thumb_url("12345", "620")
        assert url == f"{CDN_BASE}/12345_620_1.jpg"


# ── mock_color ───────────────────────────────────────────────────────────────

class TestGradientFor:
    def test_returns_gradient(self):
        g = gradient_for("620")
        assert g.startswith("linear-gradient(135deg,")

    def test_deterministic(self):
        assert gradient_for("410") == gradient_for("410")

    def test_different_codes_can_differ(self):
        codes = ["100", "200", "300", "400", "500", "600", "700", "800"]
        gradients = [gradient_for(c) for c in codes]
        assert len(set(gradients)) > 1

    def test_no_border_radius(self):
        g = gradient_for("999")
        assert "border-radius" not in g
