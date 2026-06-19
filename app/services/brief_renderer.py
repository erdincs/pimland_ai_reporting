"""Jinja2 ortamı ve brief HTML renderer."""
from __future__ import annotations

import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.utils.adl_url import urun_url, urun_thumb_url
from app.utils.mock_color import gradient_for
from app.utils.tr_format import tl, pct, num, delta_html, delta_class, delta_tri

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

_env: Environment | None = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        _env.globals.update(
            thumb_url_fn=urun_thumb_url,
            prod_url_fn=urun_url,
            gradient_fn=gradient_for,
            tl=tl,
            pct=pct,
            num=num,
            delta_html=delta_html,
            delta_class=delta_class,
            delta_tri=delta_tri,
        )
    return _env


def render_brief(template_name: str, context: dict) -> str:
    """Template adı (örn. 'brief/ec.html') ve context dict → HTML string."""
    env = _get_env()
    tmpl = env.get_template(template_name)
    return tmpl.render(**context)
