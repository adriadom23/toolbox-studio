from __future__ import annotations

from urllib.parse import urlsplit

from app.submissions.outbox import minimal_notification_payload
from app.submissions.providers import NotificationEnvelope


def validate_review_url(url: str, *, allowed_hosts: set[str]) -> str:
    parsed = urlsplit(url)
    normalized_hosts = {host.casefold() for host in allowed_hosts}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.hostname.casefold() not in normalized_hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid review URL")
    return url


def ses_message(envelope: NotificationEnvelope, *, allowed_hosts: set[str]) -> dict[str, object]:
    payload = minimal_notification_payload(envelope)
    validate_review_url(payload["review_url"], allowed_hosts=allowed_hosts)
    return {"subject": f"Toolbox Studio: {payload['project']} {payload['version']} — {payload['state']}", "metadata": payload}


def teams_message(envelope: NotificationEnvelope, *, allowed_hosts: set[str]) -> dict[str, object]:
    payload = minimal_notification_payload(envelope)
    validate_review_url(payload["review_url"], allowed_hosts=allowed_hosts)
    return {"type": "message", "metadata": payload}
