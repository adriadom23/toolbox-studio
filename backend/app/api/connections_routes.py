"""``GET /<id>/status`` — backs the rewritten ``globalConnections.js``/
``useIdentity`` probes (design.md §2.3/§4.3), blueprint prefix
``/api/connections``.

Each probe calls the corresponding VENDORED client's own session-check
function and returns ``{connected: bool, login_url: str | None}``. Every
one of these vendored functions is already built to never raise on a
down/absent session (they read Firefox-profile cookies and return a
structured false/empty result instead) — this route layer just maps that
contract onto the shape Studio's frontend expects, exactly mirroring
ToolBox's own "never a 500 on a down/absent session" convention
(design.md §4.6).
"""
from __future__ import annotations

from flask import Blueprint, jsonify

connections_bp = Blueprint("connections", __name__)


def _scc_status() -> dict:
    from vendor.integrations.scc_client import cookies_status

    status = cookies_status()
    return {"connected": bool(status.get("ready")), "login_url": status.get("login_url")}


def _mercury_status() -> dict:
    from vendor.integrations.mercury_client import mercury_login_status

    status = mercury_login_status()
    login_url = None
    try:
        from vendor.integrations.mercury_client import MERCURY_LOGIN_URL

        login_url = MERCURY_LOGIN_URL
    except ImportError:
        login_url = None
    return {"connected": bool(status.get("ready")), "login_url": login_url}


def _quicksight_status() -> dict:
    from vendor.integrations.quicksight_client import quicksight_login_status

    status = quicksight_login_status()
    login_url = None
    try:
        from vendor.integrations.quicksight_client import QUICKSIGHT_LOGIN_URL

        login_url = QUICKSIGHT_LOGIN_URL
    except ImportError:
        login_url = None
    return {"connected": bool(status.get("ready")), "login_url": login_url}


def _perfectmile_status() -> dict:
    from vendor.integrations.perfectmile_caravan_client import login_status

    status = login_status()
    return {"connected": bool(status.get("ready")), "login_url": status.get("login_url")}


_PROBES = {
    "scc": _scc_status,
    "mercury": _mercury_status,
    "quicksight": _quicksight_status,
    "perfectmile": _perfectmile_status,
}


@connections_bp.get("/<connection_id>/status")
def get_connection_status(connection_id: str):
    probe = _PROBES.get(connection_id)
    if probe is None:
        return jsonify({"error": "unknown_connection"}), 404

    try:
        result = probe()
    except Exception:  # noqa: BLE001 — a probe must never 500
        return (
            jsonify({"connected": False, "login_url": None, "error": "probe_failed"}),
            200,
        )
    return jsonify(result)
