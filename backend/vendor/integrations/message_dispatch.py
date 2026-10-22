"""Centralized helpers to send Slack/email messages with i18n support.

Tools generate their Slack text and Email HTML using
:mod:`app.integrations.messages_i18n` keys. When a tool has not yet
been fully migrated, it can still produce hardcoded Spanish prose and
this dispatcher will run a best-effort translation pass on top
(``translate_message``) so the operator sees the message in their
selected UI locale.

Usage from an API route::

    from app.integrations.message_dispatch import (
        slack_payload,
        email_payload,
    )

    text = slack_payload(make_slack_text(...), locale=locale)
    subject, body = email_payload(*make_email_html(...), locale=locale)

The helpers are intentionally minimal so they're safe to layer on
top of every tool. They never raise.
"""
from __future__ import annotations

from typing import Tuple

from app.integrations.messages_i18n import translate_message


__all__ = [
    "slack_payload",
    "email_payload",
    "translate_subject",
]


def slack_payload(text: str, *, locale: str | None) -> str:
    """Run a translation pass on a fully built Slack message."""
    if not text:
        return text
    return translate_message(text, locale)


def translate_subject(subject: str, *, locale: str | None) -> str:
    if not subject:
        return subject
    return translate_message(subject, locale)


def email_payload(
    subject: str,
    html: str,
    *,
    locale: str | None,
) -> Tuple[str, str]:
    """Translate a (subject, html) email tuple."""
    return translate_message(subject, locale), translate_message(html, locale)
