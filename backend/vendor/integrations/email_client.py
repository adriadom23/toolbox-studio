"""Station email transport: SES / SMTP first, legacy Outlook COM as fallback.

This module owns *how* an email leaves the machine. Tools build the
subject/body and call :func:`send_email`; the transport is picked from
configuration so the same code runs on the operator's Windows laptop today
and on a hosted Linux backend tomorrow.

Transport order (``EMAIL_TRANSPORT`` env var, default ``auto``):

* ``ses``     — Amazon SES via boto3 (``send_raw_email``). Needs ``EMAIL_FROM``
                and credentials/role with ``ses:SendRawEmail``. Region from
                ``SES_REGION`` or ``AWS_REGION``.
* ``smtp``    — Plain SMTP (works with the SES SMTP endpoint too). Needs
                ``SMTP_HOST`` (+ optional ``SMTP_PORT``/``SMTP_USER``/
                ``SMTP_PASS``/``EMAIL_FROM``).
* ``outlook`` — DEPRECATED. Outlook COM via pywin32; Windows + Outlook
                desktop only. Kept as a transitional fallback so stations
                keep sending email until SES/SMTP is provisioned. Remove
                once every station has a real transport configured.
* ``auto``    — Try SES if configured, then SMTP if configured, then Outlook.

Recipients accept comma and/or semicolon separated addresses; each transport
normalises to its own separator (COM wants ``;``, RFC 5322 wants ``,``).
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "send_email",
    "normalize_recipients",
    "split_email_addresses",
]


# ---------------------------------------------------------------------------
# Recipient handling (moved from ftds_rts so every tool shares one parser)
# ---------------------------------------------------------------------------


def normalize_recipients(raw: str) -> str:
    """Collapse any mix of ``,``/``;``/whitespace into ``; ``-separated.

    Email addresses never contain spaces or commas, so splitting on any
    comma / semicolon / whitespace run is safe. This also trims stray
    whitespace and drops empty tokens.
    """
    parts = [p for p in re.split(r"[;,\s]+", (raw or "").strip()) if p]
    return "; ".join(parts)


def split_email_addresses(to: str) -> list[str]:
    """Split a recipient string on comma AND semicolon into clean addresses."""
    return [addr.strip() for addr in re.split(r"[;,]+", to or "") if addr.strip()]


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def _build_mime_message(
    addresses: list[str],
    subject: str,
    body_html: str,
    attachment: Path | None,
    email_from: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = ", ".join(addresses)  # RFC 5322 wants commas
    msg["Subject"] = subject
    plain = re.sub(r"<[^>]+>", "", body_html)
    msg.set_content(plain)
    msg.add_alternative(body_html, subtype="html")
    if attachment and Path(attachment).exists():
        try:
            ctype, _enc = mimetypes.guess_type(str(attachment))
            if ctype is None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(attachment, "rb") as fh:
                msg.add_attachment(
                    fh.read(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=Path(attachment).name,
                )
        except Exception:  # noqa: BLE001
            logger.warning("could not attach %s; sending without it", attachment)
    return msg


def _ses_configured() -> bool:
    return bool(os.environ.get("EMAIL_FROM", "").strip()) and bool(
        os.environ.get("SES_REGION", "").strip() or os.environ.get("AWS_REGION", "").strip()
    )


def _send_via_ses(
    addresses: list[str], subject: str, body_html: str, attachment: Path | None
) -> bool:
    """Send through Amazon SES (``send_raw_email`` supports HTML + attachments)."""
    try:
        import boto3  # type: ignore
    except ImportError:
        logger.info("boto3 not installed; SES transport unavailable")
        return False

    email_from = os.environ.get("EMAIL_FROM", "").strip()
    region = (
        os.environ.get("SES_REGION", "").strip()
        or os.environ.get("AWS_REGION", "").strip()
    )
    if not email_from or not region:
        return False

    msg = _build_mime_message(addresses, subject, body_html, attachment, email_from)
    try:
        ses = boto3.client("ses", region_name=region)
        ses.send_raw_email(
            Source=email_from,
            Destinations=addresses,
            RawMessage={"Data": msg.as_bytes()},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("SES send failed: %s", exc)
        return False


def _smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def _send_via_smtp(
    addresses: list[str], subject: str, body_html: str, attachment: Path | None
) -> bool:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return False
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    user = os.environ.get("SMTP_USER", "").strip()
    pwd = os.environ.get("SMTP_PASS", "").strip()
    email_from = os.environ.get("EMAIL_FROM", user or "no-reply@example.com").strip()

    msg = _build_mime_message(addresses, subject, body_html, attachment, email_from)
    try:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            if user and pwd:
                smtp.login(user, pwd)
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("SMTP send failed: %s", exc)
        return False


def _send_via_outlook(
    addresses: list[str], subject: str, body_html: str, attachment: Path | None
) -> bool:
    """DEPRECATED — Outlook COM (pywin32), Windows-only transitional fallback.

    The COM dispatch is wrapped in ``CoInitialize``/``CoUninitialize``
    because Flask worker threads are not COM-initialised.
    """
    try:
        import pythoncom  # type: ignore
        import win32com.client as win32  # type: ignore
    except Exception:  # noqa: BLE001
        return False

    co_initialised = False
    try:
        try:
            pythoncom.CoInitialize()
            co_initialised = True
        except Exception:  # noqa: BLE001
            co_initialised = False
        outlook = win32.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.To = "; ".join(addresses)  # Outlook COM wants ';'
        mail.Subject = subject
        mail.HTMLBody = body_html
        if attachment and Path(attachment).exists():
            mail.Attachments.Add(str(Path(attachment).resolve()))
        mail.Send()
        logger.info(
            "email sent via DEPRECATED Outlook COM transport; configure "
            "SES/SMTP (EMAIL_TRANSPORT) to migrate off pywin32"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outlook COM send failed: %s", exc)
        return False
    finally:
        if co_initialised:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


def display_outlook_draft(
    subject: str,
    body_html: str,
    *,
    to: str = "",
    cc: str = "",
) -> bool:
    """Compose an Outlook draft and leave it OPEN on screen — never sends.

    Used by escalation flows (e.g. Forecast +3Wks → Fleet) where the
    operator reviews, fills the recipients and decides to send by hand.
    Requires Outlook desktop (pywin32); returns ``False`` when COM is not
    available so callers can surface a keyed error.
    """
    try:
        import pythoncom  # type: ignore
        import win32com.client as win32  # type: ignore
    except Exception:  # noqa: BLE001
        return False

    # Colorblind aid: drafts carry the same ✓ / ✕ as real sends.
    try:
        from app.integrations.email_colorblind import add_colorblind_symbols

        body_html = add_colorblind_symbols(body_html)
    except Exception:  # noqa: BLE001
        pass

    # Font size preference: drafts carry the same scale as real sends.
    try:
        from app.integrations.email_fontscale import apply_email_font_scale

        body_html = apply_email_font_scale(body_html)
    except Exception:  # noqa: BLE001
        pass

    co_initialised = False
    try:
        try:
            pythoncom.CoInitialize()
            co_initialised = True
        except Exception:  # noqa: BLE001
            co_initialised = False
        outlook = win32.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        if to:
            mail.To = to
        if cc:
            mail.CC = cc
        mail.Subject = subject
        mail.HTMLBody = body_html
        mail.Display(False)  # non-modal: leaves the draft window open
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outlook draft display failed: %s", exc)
        return False
    finally:
        if co_initialised:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def send_email(
    to: str,
    subject: str,
    body_html: str,
    attachment: Path | None = None,
) -> bool:
    """Send an HTML email (optional attachment) through the configured transport.

    Returns ``True`` on success. Never raises: a failed send is logged and
    reported as ``False`` so tools can surface "no se pudo enviar" without
    crashing the request.
    """
    addresses = split_email_addresses(normalize_recipients(to))
    if not addresses:
        return False

    # Translate hardcoded-Spanish subjects/bodies into the active request
    # locale (no-op for ``es`` and already-localized content).
    try:
        from app.core.locale import locale_from_request
        from app.integrations.messages_i18n import translate_message

        _loc = locale_from_request()
        subject = translate_message(subject, _loc)
        body_html = translate_message(body_html, _loc)
    except Exception:  # noqa: BLE001
        pass

    # Colorblind aid (2026-08-26): green/red numeric values get a trailing
    # ✓ / ✕ so the semaphore never depends on color alone. Central here so
    # every tool's email is covered; best-effort, never blocks a send.
    try:
        from app.integrations.email_colorblind import add_colorblind_symbols

        body_html = add_colorblind_symbols(body_html)
    except Exception:  # noqa: BLE001
        pass

    # Font size preference (Configuración ▸ Tamaños de letra ▸ Emails):
    # scale the HTML per the operator's LOCAL preference. Central here so
    # every tool's email is covered; best-effort, never blocks a send.
    try:
        from app.integrations.email_fontscale import apply_email_font_scale

        body_html = apply_email_font_scale(body_html)
    except Exception:  # noqa: BLE001
        pass

    transport = os.environ.get("EMAIL_TRANSPORT", "auto").strip().lower()

    if transport == "ses":
        return _send_via_ses(addresses, subject, body_html, attachment)
    if transport == "smtp":
        return _send_via_smtp(addresses, subject, body_html, attachment)
    if transport == "outlook":
        return _send_via_outlook(addresses, subject, body_html, attachment)

    # auto: SES -> SMTP -> Outlook (deprecated)
    if _ses_configured() and _send_via_ses(addresses, subject, body_html, attachment):
        return True
    if _smtp_configured() and _send_via_smtp(addresses, subject, body_html, attachment):
        return True
    if _send_via_outlook(addresses, subject, body_html, attachment):
        return True

    logger.warning(
        "send_email: no working transport (SES/SMTP unconfigured, Outlook "
        "unavailable); email NOT sent to %s", to
    )
    return False
