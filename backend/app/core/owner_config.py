"""Toolbox Studio's static owner/notification configuration.

Mirrors the precedent in ToolBox's own ``app/shared_excel/config_workbook.py``
(``VERSION_ADMIN_LOGINS``, a hardcoded two-login frozenset), but as PURE
STATIC CONFIGURATION rather than a runtime-identity check: Studio has no
"operator login"/SharePoint-cache concept to source a login from (design.md
§3.1/§3.2).

Config file: ``backend/config/studio.config.json`` (git-ignored — a fresh
checkout never has one). ``studio.config.example.json`` (committed, same
directory) is the template; copy it to ``studio.config.json`` to customize.
Missing/malformed config falls back to the in-code two-login default below,
so a fresh checkout still has a sane "who gets notified" list.

``.env`` overrides (``STUDIO_OWNER_EMAILS`` comma-separated,
``STUDIO_SLACK_WEBHOOK``) take precedence over the JSON file's values when
set, per NFR-3's "``.env`` file and/or a small JSON/YAML config file"
framing — this lets a deployment set secrets via environment without
committing them to ``studio.config.json``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.core.studio_config import DEFAULT_CONFIG_PATH

# Default owner list — mirrors ``VERSION_ADMIN_LOGINS`` exactly, per FR-4 and
# NFR-3. This is documentation of who gets notified, not an access-control
# gate: Studio does not authenticate developers against it.
DEFAULT_OWNERS: tuple[str, ...] = ("adriadom", "zesparzj")

DEFAULT_CONFIG: dict[str, Any] = {
    "owners": list(DEFAULT_OWNERS),
    "notifications": {
        "email": {"enabled": True, "to": [], "transport": "smtp"},
        "slack": {"enabled": False, "webhook_url": ""},
    },
}

CONFIG_DIR = DEFAULT_CONFIG_PATH.parent
CONFIG_PATH = DEFAULT_CONFIG_PATH


def _config_path() -> Path:
    """Return the configured path, honoring ``STUDIO_CONFIG_PATH`` for tests."""

    override = os.environ.get("STUDIO_CONFIG_PATH", "").strip()
    return Path(override) if override else CONFIG_PATH


def load_owner_config() -> dict[str, Any]:
    """Return the effective owner/notification config as a dict.

    Reads ``studio.config.json`` when present and well-formed; falls back to
    :data:`DEFAULT_CONFIG` when the file is missing, unreadable, or contains
    invalid JSON — never raises.
    """

    path = _config_path()
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return data


def notification_targets() -> dict[str, Any]:
    """Return ``{"emails": [...], "slack_webhook": str | None}``.

    Values come from ``studio.config.json`` (or the in-code default when
    absent), overridden by ``STUDIO_OWNER_EMAILS``/``STUDIO_SLACK_WEBHOOK``
    env vars when set. ``emails`` is empty when
    ``notifications.email.enabled`` is false; ``slack_webhook`` is ``None``
    when ``notifications.slack.enabled`` is false or no webhook URL is
    configured.
    """

    config = load_owner_config()
    notifications = config.get("notifications") or {}
    email_cfg = notifications.get("email") or {}
    slack_cfg = notifications.get("slack") or {}

    emails: list[str] = []
    if email_cfg.get("enabled"):
        emails = [str(addr).strip() for addr in (email_cfg.get("to") or []) if str(addr).strip()]

    env_emails = os.environ.get("STUDIO_OWNER_EMAILS", "").strip()
    if env_emails:
        emails = [addr.strip() for addr in env_emails.split(",") if addr.strip()]

    slack_webhook: str | None = None
    if slack_cfg.get("enabled"):
        webhook = str(slack_cfg.get("webhook_url") or "").strip()
        slack_webhook = webhook or None

    env_webhook = os.environ.get("STUDIO_SLACK_WEBHOOK", "").strip()
    if env_webhook:
        slack_webhook = env_webhook

    return {"emails": emails, "slack_webhook": slack_webhook}


def owners() -> list[str]:
    """Return the ``owners`` list (who gets notified), falling back to the
    two-login default when the config has none."""

    config = load_owner_config()
    raw = config.get("owners")
    if not raw:
        return list(DEFAULT_OWNERS)
    return [str(login).strip() for login in raw if str(login).strip()]
