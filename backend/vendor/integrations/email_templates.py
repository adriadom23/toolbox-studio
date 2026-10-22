"""Shared HTML email styling for every ToolBox tool.

The legacy ``Waveplan.py`` had a clean Amazon-themed email layout (dark
navy header, Amazon-orange accents, zebra rows, rounded corners, soft
borders). All migrated tools used to ship their own one-off HTML and
the result felt inconsistent. This module centralizes the styling so
every tool sends Outlook-friendly emails that look the same.

Usage::

    from app.integrations.email_templates import (
        render_email,
        section,
        kpi_grid,
        styled_table,
    )

    body = render_email(
        title="Bolsas | CDOR | 22/05/2026",
        intro_html="<p>Hola CDOR,</p><p>Resumen de bolsas de hoy.</p>",
        sections=[
            section("Resumen", kpi_grid([("Rutas", 12), ("Bolsas", 35)])),
            section(
                "Detalle por ruta",
                styled_table(["Ruta", "Bolsas"], [["A1", 3], ["A2", 5]]),
            ),
        ],
        signoff_html="<p>Saludos,<br>Ops Team</p>",
    )

The helpers return strings so they're trivially composable. They emit
inline CSS only (Outlook strips ``<style>`` tags from the body) so the
look survives in Outlook desktop, OWA and Gmail.
"""
from __future__ import annotations

import html as _html
from typing import Any, Iterable, Sequence
from urllib.parse import quote as _url_quote

# Amazon-style colour palette (kept in sync with the legacy Waveplan
# ``create_wave_plan_table_html`` / ``create_combined_capacity_table_html``).
COLOR_HEADER_BG = "#232F3E"
COLOR_HEADER_TEXT = "#ffffff"
COLOR_ACCENT = "#FF9900"
COLOR_TEXT = "#1a1a2e"
COLOR_TEXT_MUTED = "#5c6873"
COLOR_BORDER = "#e9ecef"
COLOR_BORDER_STRONG = "#dee2e6"
COLOR_ZEBRA = "#f8f9fa"

COLOR_OK_BG = "#d4edda"
COLOR_OK_TEXT = "#155724"
COLOR_WARN_BG = "#fef3cd"
COLOR_WARN_TEXT = "#856404"
COLOR_BAD_BG = "#fde8e8"
COLOR_BAD_TEXT = "#721c24"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _safe_html(value: Any) -> str:
    """Return the value as-is when it's already HTML, otherwise escaped."""
    return value if isinstance(value, str) else _esc(value)


def tracking_link(tid: Any) -> str:
    """Render a tracking ID as an escaped SCC package-detail link."""
    clean = str(tid or "").strip()
    if not clean:
        return ""
    url = (
        "https://logistics.amazon.co.uk/station/dashboard/search?"
        "shareableLink=detailPage%2F"
        + _url_quote(clean, safe="")
    )
    return (
        f'<a href="{_esc(url)}" style="color:{COLOR_ACCENT};'
        f'text-decoration:none">{_esc(clean)}</a>'
    )


# ---------------------------------------------------------------------------
# Section + KPI helpers
# ---------------------------------------------------------------------------


def section(title: str, html_content: str, *, eyebrow: str = "") -> str:
    """Wrap ``html_content`` in a styled section with a heading bar."""
    eyebrow_html = (
        f"<p style='margin:0 0 4px 0;color:{COLOR_TEXT_MUTED};"
        f"font-size:11px;text-transform:uppercase;letter-spacing:0.04em;"
        f"font-weight:700;font-family:Segoe UI,Arial,sans-serif'>{_esc(eyebrow)}</p>"
        if eyebrow
        else ""
    )
    return (
        "<div style='margin:0 0 22px 0;font-family:Segoe UI,Arial,sans-serif'>"
        f"{eyebrow_html}"
        f"<h3 style='margin:0 0 10px 0;color:{COLOR_TEXT};font-size:15px;"
        "font-weight:700;border-left:3px solid "
        f"{COLOR_ACCENT};padding-left:10px'>"
        f"{_esc(title)}</h3>"
        f"<div>{html_content}</div>"
        "</div>"
    )


def kpi_grid(items: Sequence[tuple[str, Any]]) -> str:
    """Render ``[(label, value), ...]`` as a row of stat cards.

    Falls back gracefully when there are many items by wrapping with
    ``flex-wrap``. Outlook ignores flexbox but the table fallback we
    inline keeps the cards on one row when there's space.
    """
    if not items:
        return ""
    cells = []
    for label, value in items:
        cells.append(
            "<td style='padding:10px 14px;background:"
            f"{COLOR_ZEBRA};border:1px solid {COLOR_BORDER};"
            "border-radius:6px;text-align:center;min-width:120px'>"
            f"<div style='color:{COLOR_TEXT_MUTED};font-size:11px;"
            "font-weight:700;text-transform:uppercase;letter-spacing:0.04em'>"
            f"{_esc(label)}</div>"
            f"<div style='color:{COLOR_TEXT};font-size:18px;"
            f"font-weight:700;margin-top:2px'>{_safe_html(value)}</div>"
            "</td>"
        )
    return (
        "<table role='presentation' cellpadding='0' cellspacing='6' style="
        f"'border-collapse:separate;border-spacing:6px 0;font-family:Segoe UI,Arial,sans-serif;"
        "font-size:13px;margin:0 0 4px 0'>"
        f"<tr>{''.join(cells)}</tr></table>"
    )


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def styled_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    align: Sequence[str] | None = None,
    row_tag: callable | None = None,
    empty_text: str = "Sin datos",
) -> str:
    """Outlook-friendly Amazon-themed table.

    * ``align`` — per-column alignment (``"left" | "center" | "right"``).
    * ``row_tag`` — optional callable ``(row) -> "ok" | "warn" | "bad" | None``
      that paints the row a soft green / yellow / red.
    """
    rows_list = [list(r) for r in rows]
    if not rows_list:
        return (
            f"<p style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
            f"color:{COLOR_TEXT_MUTED};margin:0'>"
            f"{_esc(empty_text)}</p>"
        )

    align = list(align or ["left"] * len(headers))
    if len(align) < len(headers):
        align += ["left"] * (len(headers) - len(align))

    th_cells = []
    for idx, header in enumerate(headers):
        th_cells.append(
            f"<th style='background:{COLOR_HEADER_BG};color:{COLOR_HEADER_TEXT};"
            "padding:10px 12px;text-align:" + _esc(align[idx]) + ";"
            "font-weight:600;border:0;border-bottom:2px solid "
            f"{COLOR_ACCENT};font-size:12px;font-family:Segoe UI,Arial,sans-serif'>"
            f"{_esc(header)}</th>"
        )
    body_rows: list[str] = []
    for row_idx, row in enumerate(rows_list):
        zebra = COLOR_ZEBRA if row_idx % 2 == 1 else "#ffffff"
        tag = row_tag(row) if row_tag else None
        bg = zebra
        color = COLOR_TEXT
        if tag == "ok":
            bg = COLOR_OK_BG
            color = COLOR_OK_TEXT
        elif tag == "warn":
            bg = COLOR_WARN_BG
            color = COLOR_WARN_TEXT
        elif tag == "bad":
            bg = COLOR_BAD_BG
            color = COLOR_BAD_TEXT
        cells = []
        for idx, value in enumerate(row):
            cells.append(
                f"<td style='border:1px solid {COLOR_BORDER};padding:8px 12px;"
                f"text-align:{_esc(align[idx])};color:{color};font-size:12px;"
                "font-family:Segoe UI,Arial,sans-serif'>"
                f"{_safe_html(value)}</td>"
            )
        body_rows.append(
            f"<tr style='background:{bg}'>" + "".join(cells) + "</tr>"
        )

    return (
        "<table role='presentation' cellpadding='0' cellspacing='0' "
        "style='border-collapse:collapse;width:100%;margin:0 0 18px 0;"
        f"border-radius:6px;overflow:hidden;border:1px solid {COLOR_BORDER_STRONG};"
        "font-family:Segoe UI,Arial,sans-serif;font-size:12px'>"
        f"<thead><tr>{''.join(th_cells)}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def detail_table(rows: Sequence[tuple[str, Any]]) -> str:
    """Two-column "label | value" detail table (used for per-row breakdowns)."""
    return styled_table(["Campo", "Valor"], rows, align=["left", "left"])


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def callout(text: str, *, tone: str = "info") -> str:
    """Coloured callout block (info / ok / warn / bad)."""
    palette = {
        "info": ("#0d3b66", "#eaf3ff", "#0d3b66"),
        "ok": (COLOR_OK_TEXT, COLOR_OK_BG, COLOR_OK_TEXT),
        "warn": (COLOR_WARN_TEXT, COLOR_WARN_BG, COLOR_WARN_TEXT),
        "bad": (COLOR_BAD_TEXT, COLOR_BAD_BG, COLOR_BAD_TEXT),
    }
    border, bg, text_color = palette.get(tone, palette["info"])
    return (
        f"<div style='border-left:4px solid {border};background:{bg};"
        f"color:{text_color};padding:10px 14px;border-radius:4px;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
        "margin:0 0 16px 0'>"
        f"{_safe_html(text)}</div>"
    )


def render_email(
    *,
    title: str,
    intro_html: str = "",
    sections: Sequence[str] = (),
    signoff_html: str = "",
    eyebrow: str = "ToolBox",
    preheader: str = "",
    footer_text: str | None = None,
) -> str:
    """Wrap the sections in the shared Amazon-themed envelope."""
    safe_title = _esc(title)
    preheader_html = (
        f"<span style='display:none;visibility:hidden;opacity:0;color:transparent;"
        f"max-height:0;max-width:0;overflow:hidden;'>{_esc(preheader)}</span>"
        if preheader
        else ""
    )
    parts: list[str] = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'></head>",
        "<body style='margin:0;padding:0;background:#f4f5f7;"
        f"color:{COLOR_TEXT};font-family:Segoe UI,Arial,sans-serif'>",
        preheader_html,
        "<table role='presentation' cellpadding='0' cellspacing='0' "
        "width='100%' style='background:#f4f5f7;padding:24px 0'>",
        "<tr><td align='center'>",
        "<table role='presentation' cellpadding='0' cellspacing='0' "
        "width='720' style='max-width:720px;background:#ffffff;"
        "border-radius:10px;overflow:hidden;"
        f"border:1px solid {COLOR_BORDER_STRONG};"
        "box-shadow:0 4px 14px rgba(15,22,32,0.06)'>",
        # Header band
        "<tr><td style='background:" + COLOR_HEADER_BG + ";"
        f"padding:18px 28px;border-bottom:3px solid {COLOR_ACCENT}'>",
        f"<div style='color:#dde2eb;font-size:11px;font-weight:700;"
        "letter-spacing:0.08em;text-transform:uppercase;font-family:Segoe UI,Arial,sans-serif'>"
        f"{_esc(eyebrow)}</div>",
        f"<div style='color:{COLOR_HEADER_TEXT};font-size:20px;font-weight:700;"
        "font-family:Segoe UI,Arial,sans-serif;margin-top:2px'>"
        f"{safe_title}</div>",
        "</td></tr>",
        # Body
        "<tr><td style='padding:24px 28px'>",
    ]
    if intro_html:
        parts.append(
            "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
            f"color:{COLOR_TEXT};line-height:1.5;margin:0 0 18px 0'>{intro_html}</div>"
        )
    for sec in sections:
        if sec:
            parts.append(sec)
    if signoff_html:
        parts.append(
            "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
            f"color:{COLOR_TEXT};line-height:1.5;margin:18px 0 0 0;"
            f"border-top:1px solid {COLOR_BORDER};padding-top:14px'>"
            f"{signoff_html}</div>"
        )
    parts.extend(
        [
            "</td></tr>",
            # Footer
            "<tr><td style='background:#fafbfc;padding:12px 28px;"
            f"border-top:1px solid {COLOR_BORDER}'>",
            f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:11px;"
            f"color:{COLOR_TEXT_MUTED}'>"
            + _esc(
                footer_text
                if footer_text is not None
                else "Mensaje generado por ToolBox \u00b7 revisa el adjunto si lo hubiera"
            )
            + "</div></td></tr>",
            "</table></td></tr></table>",
            "</body></html>",
        ]
    )
    return "\n".join(parts)


__all__ = [
    "callout",
    "detail_table",
    "kpi_grid",
    "render_email",
    "section",
    "styled_table",
    "tracking_link",
    "COLOR_ACCENT",
    "COLOR_BORDER",
    "COLOR_HEADER_BG",
    "COLOR_HEADER_TEXT",
    "COLOR_OK_BG",
    "COLOR_OK_TEXT",
    "COLOR_TEXT",
    "COLOR_TEXT_MUTED",
    "COLOR_WARN_BG",
    "COLOR_WARN_TEXT",
    "COLOR_ZEBRA",
]
