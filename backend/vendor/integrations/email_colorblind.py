"""Colorblind aid for outgoing email HTML: append a check / cross to values.

Instrucción del usuario (2026-08-26): la ayuda visual ✓ (bien) / ✕ (mal) va
en TODOS los scripts, también en los emails — los colores se quedan, el
símbolo es una pista extra para daltónicos. Los mensajes de Slack (texto
plano) no se tocan.

This is the single central post-processor: :func:`add_colorblind_symbols`
runs inside :func:`app.integrations.email_client.send_email` (every tool's
email bottoms out there) and :func:`display_outlook_draft`, so no individual
email builder needs to add symbols by hand. The rule mirrors the frontend
DOM helper (``frontend/src/shared/semaphoreSymbols.js``), which decorates
the in-app previews the same way:

* a LEAF element (``td``/``span``/... with no nested tags),
* whose inline ``color:`` (or a ``good``/``bad`` class, daily.py style)
  is one of the known semaphore greens/reds,
* and whose text looks like a NUMERIC value (numbers, separators, %),
* gets ``" ✓"`` / ``" ✕"`` appended — unless it already carries a symbol
  (idempotent, safe on builders that add their own).

Colored words ("OK", sentences, error banners) are deliberately skipped:
only values need the aid, decorating prose would be noise.
"""
from __future__ import annotations

import html as _html
import re

__all__ = ["add_colorblind_symbols", "GOOD_COLORS", "BAD_COLORS"]

# Semaphore text colors used across the email builders (email_templates
# COLOR_OK_TEXT/COLOR_BAD_TEXT, FlowMeetingNs, daily.py chips, dpr.py bands,
# metis_plan deltas, handover targets) plus the UI theme greens/reds so
# frontend-composed bodies stay covered. Keep in sync with the palette in
# frontend/src/shared/semaphoreSymbols.js.
GOOD_COLORS = frozenset({
    "#155724", "#1f6f49", "#27ae60", "#4b8f5c", "#6ec48a", "#1e7c3a",
    "#2ea84a", "#1d8102", "#157347", "#16a34a", "#15803d",
})
BAD_COLORS = frozenset({
    "#721c24", "#b93838", "#c0392b", "#b34b4b", "#e07b7b", "#b3261e",
    "#d63b3b", "#c7511f", "#dc2626",
})

GOOD_SYMBOL = " \u2713"  # ✓
BAD_SYMBOL = " \u2715"   # ✕

# Leaf elements only: the inner content contains no nested tags, so the
# symbol lands right next to the value the color belongs to.
_LEAF_RE = re.compile(
    r"<(?P<tag>td|th|span|div|b|strong|em|small|p|li)\b(?P<attrs>[^>]*)>"
    r"(?P<inner>[^<]*)</(?P=tag)>",
    re.IGNORECASE,
)

# ``style`` attribute with single or double quotes (both used by builders).
_STYLE_RE = re.compile(r"style\s*=\s*(?P<q>['\"])(?P<css>.*?)(?P=q)", re.IGNORECASE | re.DOTALL)
_CLASS_RE = re.compile(r"class\s*=\s*(?P<q>['\"])(?P<names>.*?)(?P=q)", re.IGNORECASE | re.DOTALL)

# Standalone ``color:`` — the lookbehind rejects background-color /
# border-color / -webkit-text-fill-color etc.
_TEXT_COLOR_RE = re.compile(r"(?<![-\w])color\s*:\s*(#[0-9a-fA-F]{6})")

# Numeric-looking value: optional sign, digits with . , or space separators,
# optional trailing %. (Mirrors the frontend helper.)
_NUMERIC_RE = re.compile(r"^[+\-\u2212\u00b1]?\d[\d.,\s\u00a0]*%?$")

# Already decorated (builder-side or a previous pass) — never double up.
_HAS_SYMBOL_RE = re.compile(r"[\u2713\u2715\u2717\u2714\u2716\u274c]\s*$")


def _classify_attrs(attrs: str) -> str:
    """Return ``"good"`` / ``"bad"`` / ``""`` for an element's attributes."""
    if "data-no-semaphore" in attrs:
        return ""
    style = _STYLE_RE.search(attrs)
    if style:
        m = _TEXT_COLOR_RE.search(style.group("css"))
        if m:
            color = m.group(1).lower()
            if color in GOOD_COLORS:
                return "good"
            if color in BAD_COLORS:
                return "bad"
            return ""
    cls = _CLASS_RE.search(attrs)
    if cls:
        names = cls.group("names").lower().split()
        # daily.py legacy semaphore classes (.good / .bad <style> block).
        if "good" in names:
            return "good"
        if "bad" in names:
            return "bad"
    return ""


def _is_value_text(inner: str) -> bool:
    text = _html.unescape(inner).replace("\u00a0", " ").strip()
    if not text or len(text) > 24:
        return False
    if _HAS_SYMBOL_RE.search(text):
        return False
    return bool(_NUMERIC_RE.match(text))


def _decorate(match: re.Match) -> str:
    sem = _classify_attrs(match.group("attrs"))
    if not sem:
        return match.group(0)
    inner = match.group("inner")
    if not _is_value_text(inner):
        return match.group(0)
    symbol = GOOD_SYMBOL if sem == "good" else BAD_SYMBOL
    # Keep trailing whitespace outside the value (symbol hugs the number).
    stripped = inner.rstrip()
    tail = inner[len(stripped):]
    return (
        f"<{match.group('tag')}{match.group('attrs')}>"
        f"{stripped}{symbol}{tail}</{match.group('tag')}>"
    )


def add_colorblind_symbols(html: str) -> str:
    """Append ✓ / ✕ next to green/red numeric values in email HTML.

    Pure and idempotent: running it twice yields the same output, and any
    parsing surprise leaves the original fragment untouched (regex only
    rewrites the leaf elements it fully matched).
    """
    if not html or "<" not in html:
        return html
    try:
        return _LEAF_RE.sub(_decorate, html)
    except Exception:  # noqa: BLE001 — never break an email over the aid
        return html
