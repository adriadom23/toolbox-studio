"""Escala el tamaño de letra del HTML de los emails enviados.

Configuración ▸ Tamaños de letra ▸ sección "Emails": si el operador puso
p. ej. 120%, los emails que envía la Toolbox desde este equipo salen con
la letra un 20% más grande. Mecanismo CENTRAL en los dos choke points de
``email_client`` (``send_email`` y ``display_outlook_draft``), igual que
``email_colorblind`` — los builders por herramienta NO se tocan.

Cómo escala:
* multiplica todos los ``font-size: Npx|pt`` inline del HTML, y
* si el HTML es un fragmento (sin ``<body>``), lo envuelve en un ``div``
  con la base escalada para que el texto SIN font-size explícito también
  crezca (los clientes de correo heredan del contenedor).

Best-effort: cualquier fallo devuelve el HTML original y nunca bloquea un
envío. Con la escala al 100% (o sin preferencia guardada) es un no-op.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_FONT_SIZE_RE = re.compile(r"(font-size\s*:\s*)(\d+(?:\.\d+)?)\s*(px|pt)", re.IGNORECASE)
_HAS_BODY_RE = re.compile(r"<body\b", re.IGNORECASE)

# Tamaño base de los clientes de correo para texto sin font-size explícito.
_BASE_PX = 16


def scale_html_font_sizes(html: str, scale: float) -> str:
    """Devuelve ``html`` con los tamaños de letra multiplicados por ``scale``.

    NO es idempotente (dos pasadas escalan dos veces): se llama UNA vez por
    envío/draft en los choke points de ``email_client``.
    """
    if not html or not isinstance(html, str):
        return html
    try:
        factor = float(scale)
    except (TypeError, ValueError):
        return html
    if not (0.1 <= factor <= 10) or abs(factor - 1.0) < 0.005:
        return html

    def _multiply(match: re.Match) -> str:
        value = round(float(match.group(2)) * factor, 2)
        text = f"{value:g}"
        return f"{match.group(1)}{text}{match.group(3)}"

    scaled = _FONT_SIZE_RE.sub(_multiply, html)

    # Fragmentos (la mayoría de builders): un wrapper con la base escalada
    # cubre el texto que no declara font-size. Documentos completos con
    # <body> no se envuelven (sería HTML inválido); sus tamaños inline ya
    # quedaron multiplicados arriba.
    if not _HAS_BODY_RE.search(scaled):
        base = max(8, round(_BASE_PX * factor))
        scaled = f'<div style="font-size:{base}px;">{scaled}</div>'
    return scaled


def apply_email_font_scale(html: str) -> str:
    """Aplica la escala de la sección "emails" de las preferencias locales."""
    try:
        from app.core.font_prefs import email_font_scale

        return scale_html_font_sizes(html, email_font_scale())
    except Exception:  # noqa: BLE001 — nunca bloquear un envío
        logger.debug("email font scale skipped", exc_info=True)
        return html
