"""Helpers to extract the requesting locale from a Flask request.

Every Slack/email build path on the backend can be translated when
the frontend forwards the active UI locale (``es`` / ``en`` / ``fr`` /
``de`` / ``it``) in the request body. ``locale_from_request`` and
``locale_from_payload`` extract it consistently and fall back to
``"es"`` for backwards compatibility with older clients.
"""
from __future__ import annotations

from typing import Any

from flask import request

from app.integrations.messages_i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    normalize_locale,
)


__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "locale_from_payload",
    "locale_from_request",
]


def locale_from_payload(payload: Any) -> str:
    """Return a normalized locale from a JSON payload (or default)."""
    if isinstance(payload, dict):
        for key in ("locale", "lang", "language"):
            value = payload.get(key)
            if value:
                return normalize_locale(value)
    return DEFAULT_LOCALE


def locale_from_request() -> str:
    """Inspect the active Flask ``request`` (body, query, headers).

    Order:
      1. JSON body ``locale``/``lang``/``language``.
      2. Form value (multipart uploads).
      3. ``?locale=`` query parameter.
      4. ``X-Toolbox-Locale`` header.
      5. ``DEFAULT_LOCALE``.
    """
    try:
        json_body = request.get_json(silent=True)
    except Exception:  # noqa: BLE001
        json_body = None
    locale = locale_from_payload(json_body)
    if locale != DEFAULT_LOCALE:
        return locale

    try:
        for key in ("locale", "lang", "language"):
            value = request.form.get(key)
            if value:
                return normalize_locale(value)
    except Exception:  # noqa: BLE001
        pass

    try:
        for key in ("locale", "lang", "language"):
            value = request.args.get(key)
            if value:
                return normalize_locale(value)
    except Exception:  # noqa: BLE001
        pass

    try:
        header = request.headers.get("X-Toolbox-Locale", "")
        if header:
            return normalize_locale(header)
    except Exception:  # noqa: BLE001
        pass

    return DEFAULT_LOCALE
