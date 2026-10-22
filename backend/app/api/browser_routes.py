"""Open external login URLs in the operator's Firefox, regardless of which
browser hosts the Studio SPA, blueprint prefix ``/api/browser``.

Mirrors ToolBox's own ``app/api/browser_routes.py`` (same route, same
request/response shape), trimmed to the one endpoint Studio's frontend
actually calls: ``shared/api/http.js``'s ``openInFirefox()``, used by
``shared/openLoginTab.js`` whenever the host browser is not Firefox (or a
same-browser ``window.open`` was popup-blocked). ToolBox's richer
self-closing "identify hub" page and its ``/firefox-status`` probe are
out of scope — Studio has no multi-tab-leader/shared-login-tab concern
(see VENDORING.md's note on ``globalConnections.js``), and nothing in
Studio's frontend calls either of those two routes.

The vendored integration clients authenticate by reading Amazon SSO
cookies straight from the operator's Firefox profile (see
``vendor.integrations.firefox_cookies``), so every identification/login
tab MUST land in Firefox — otherwise the operator logs in on another
browser and the backend never sees the session. A web page cannot launch
a different browser from a sandboxed tab, but this local Flask server
can, via ``vendor.integrations.selenium_client.resolve_firefox_binary``
(already vendored — FEAT-001).
"""
from __future__ import annotations

import subprocess
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

browser_bp = Blueprint("browser", __name__)


def _is_safe_http_url(url: str) -> bool:
    """Only allow real ``http(s)`` URLs so the endpoint can't be coaxed into
    handing arbitrary schemes (``file://``, custom protocol handlers, ...) to
    the Firefox command line."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@browser_bp.post("/open-firefox")
def open_firefox():
    """Open ``url`` in the operator's Firefox (new tab, reusing the profile).

    Body: ``{"url": "https://..."}``.

    Returns ``{ok, opened, error}``. ``opened`` is ``False`` (with HTTP 200)
    when Firefox is not installed, so the SPA can gracefully fall back to
    opening the tab in the current browser instead of leaving the operator
    stuck — exactly the fallback ``shared/openLoginTab.js`` already
    implements on the frontend side of this same call.
    """
    from vendor.integrations.selenium_client import resolve_firefox_binary

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not _is_safe_http_url(url):
        return jsonify({"ok": False, "opened": False, "error": "invalid_url"}), 400

    firefox_bin = resolve_firefox_binary()
    if not firefox_bin:
        return jsonify({"ok": True, "opened": False, "error": "firefox_not_found"})

    try:
        # ``-new-tab`` reuses the already-running Firefox window (the one
        # whose profile holds the operator's SSO cookies) instead of
        # spawning a second instance. Detached: we never wait on it.
        subprocess.Popen(
            [firefox_bin, "-new-tab", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:  # pragma: no cover - launch failure is environment-specific
        return jsonify({"ok": False, "opened": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "opened": True})
