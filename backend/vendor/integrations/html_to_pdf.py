"""HTML → PDF helper with graceful fallbacks.

Order of attempts:

1. ``xhtml2pdf`` (pure-Python, no native deps) — this is the path that
   actually runs in CI / Windows / bundled exe. Best for the styled
   email templates ``email_templates`` produces.
2. ``pdfkit`` (requires the ``wkhtmltopdf`` binary on PATH or one of
   the well-known install paths under ``Program Files``). Higher
   fidelity if the operator has it installed.
3. ``weasyprint`` (requires GTK/Pango/Cairo native libs). Used only
   when the operator has the dependencies set up properly — usually
   on Linux.

Returns the raw PDF bytes on success, ``None`` when none of the
backends managed to produce a file. Callers should treat ``None`` as
"send the HTML inline" and continue without crashing.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional


_log = logging.getLogger("toolbox.html_to_pdf")


def _try_xhtml2pdf(html: str) -> Optional[bytes]:
    try:
        from xhtml2pdf import pisa
    except Exception as exc:  # pragma: no cover - import path
        _log.debug("xhtml2pdf unavailable: %s", exc)
        return None
    try:
        buffer = io.BytesIO()
        result = pisa.CreatePDF(src=html, dest=buffer, encoding="utf-8")
        if result.err:
            _log.warning("xhtml2pdf reported %d errors", result.err)
            return None
        return buffer.getvalue()
    except Exception:
        _log.exception("xhtml2pdf failed")
        return None


def _wkhtmltopdf_paths() -> list[str]:
    return [
        os.environ.get("WKHTMLTOPDF") or "",
        os.environ.get("WKHTMLTOPDF_PATH") or "",
        r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
        r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe",
        "/usr/bin/wkhtmltopdf",
        "/usr/local/bin/wkhtmltopdf",
    ]


def _try_pdfkit(html: str) -> Optional[bytes]:
    try:
        import pdfkit
    except Exception as exc:  # pragma: no cover - import path
        _log.debug("pdfkit unavailable: %s", exc)
        return None
    try:
        cfg = None
        for candidate in _wkhtmltopdf_paths():
            if candidate and os.path.exists(candidate):
                cfg = pdfkit.configuration(wkhtmltopdf=candidate)
                break
        if cfg is None:
            try:
                cfg = pdfkit.configuration()
            except Exception:
                cfg = None
        if cfg is None:
            return None
        return pdfkit.from_string(html, False, configuration=cfg)
    except Exception:
        _log.exception("pdfkit failed")
        return None


def _try_weasyprint(html: str) -> Optional[bytes]:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception as exc:  # pragma: no cover - import path
        _log.debug("weasyprint unavailable: %s", exc)
        return None
    try:
        return HTML(string=html).write_pdf()
    except Exception:
        _log.exception("weasyprint failed")
        return None


def html_to_pdf_bytes(html: str) -> Optional[bytes]:
    """Render ``html`` to PDF bytes. Returns ``None`` on failure.

    Order: xhtml2pdf (always available) → pdfkit → weasyprint.
    """
    if not html:
        return None
    for fn in (_try_xhtml2pdf, _try_pdfkit, _try_weasyprint):
        out = fn(html)
        if out:
            return out
    return None


def write_pdf(html: str, dest: Path) -> bool:
    """Write the rendered PDF to ``dest``. Returns True on success."""
    data = html_to_pdf_bytes(html)
    if not data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


__all__ = ["html_to_pdf_bytes", "write_pdf"]
