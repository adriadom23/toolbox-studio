"""Read-only exposure of Studio's owner/notification configuration,
blueprint prefix ``/api/config``.

FEAT-004 implementation-level addition (not a new feature): the design
(§3.2) specifies that the "Enviar código" confirmation dialog shows the
configured owner list ("This will notify: adriadom, zesparzj") and warns
when both notification channels are disabled, but §4.3 did not name a
route exposing that config read-only. FEAT-004's own steps explicitly
permit adding one small GET route for it. This is a thin wrapper over the
vendored-free, already-present ``app.core.owner_config`` helpers — no new
transport or config mechanism is introduced.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from app.core import owner_config

config_bp = Blueprint("config", __name__)


@config_bp.get("/owners")
def get_owners():
    """Return who gets notified and which channels are enabled.

    Shape consumed by the frontend's submit dialog:
    ``{owners: [...], email_enabled: bool, slack_enabled: bool}``.
    ``*_enabled`` reflects the EFFECTIVE config (JSON file + ``.env``
    overrides via ``notification_targets()``): email is "enabled" when at
    least one recipient resolves, slack when a webhook URL resolves. This
    keeps the dialog's "both channels disabled" warning honest regardless
    of whether the destinations come from the JSON file or env vars.
    """

    targets = owner_config.notification_targets()
    return jsonify(
        {
            "owners": owner_config.owners(),
            "email_enabled": bool(targets.get("emails")),
            "slack_enabled": bool(targets.get("slack_webhook")),
        }
    )
