"""STEM (Sort Tech Equipment Management) client — stem-eu.corp.amazon.com.

Downloads the live equipment list for a node using the operator's Firefox
Federate session, the same way the equipment dashboard does it:

* ``GET  /sortcenter/equipmentmanagement/csrfToken``  -> per-session token
* ``POST /graphql`` with header ``anti-csrftoken-a2z: <token>`` and the
  ``Resources(nodeId)`` operation -> every resource of the station
  (DOCK_DOOR, STACKING_AREA, STAGING_AREA, GENERAL_AREA, ...), i.e. the
  same rows the legacy ``STEM_<STATION>.csv`` export contains.

Auth uses the ``federate_id_token`` cookie from Firefox. When the session
is expired the operator opens the login URL (the equipment page pops the
Federate login) and retries.
"""
from __future__ import annotations

import logging
import os

import requests

from app.integrations.firefox_cookies import read_cookies_from_profile

logger = logging.getLogger(__name__)

STEM_HOST = os.environ.get("STEM_EU_HOST", "stem-eu.corp.amazon.com")


def login_url(station: str) -> str:
    """Equipment page for the node — opening it triggers the Federate login."""
    return f"https://{STEM_HOST}/node/{station}/equipment"


class StemClientError(RuntimeError):
    """Network / unexpected-payload error talking to STEM."""


class StemAuthError(StemClientError):
    """The Firefox Federate session for STEM is missing or expired."""


_RESOURCES_QUERY = """query Resources($nodeId: String!) {
  resources(nodeId: $nodeId) {
    resourceId
    resourceType
    label
    status
    resourceAttributes { key value }
    userLogin
    lastUpdateTime
    resourceGroup
  }
}"""


def _read_cookies() -> dict[str, str]:
    try:
        return read_cookies_from_profile([STEM_HOST, "corp.amazon.com"], target_host=STEM_HOST)
    except Exception as exc:  # noqa: BLE001 - no profile / no cookies DB.
        raise StemAuthError(str(exc)) from exc


def _session() -> requests.Session:
    session = requests.Session()
    session.cookies.update(_read_cookies())
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    return session


def _fetch_csrf_token(session: requests.Session, timeout: float) -> str:
    try:
        resp = session.get(
            f"https://{STEM_HOST}/sortcenter/equipmentmanagement/csrfToken",
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise StemClientError(f"csrfToken request failed: {exc}") from exc
    # An expired Federate session bounces to the IdP (302) or serves HTML.
    if resp.status_code != 200 or "<html" in resp.text[:200].lower():
        raise StemAuthError(f"csrfToken status {resp.status_code}")
    token = resp.text.strip()
    if not token:
        raise StemAuthError("empty csrfToken")
    return token


def fetch_resources(station: str, timeout: float = 30.0) -> list[dict]:
    """Return the live resource list for ``station`` (node id).

    Raises :class:`StemAuthError` when the Firefox session is not valid and
    :class:`StemClientError` on transport/payload problems.
    """
    session = _session()
    token = _fetch_csrf_token(session, timeout)
    try:
        resp = session.post(
            f"https://{STEM_HOST}/graphql",
            json={
                "operationName": "Resources",
                "query": _RESOURCES_QUERY,
                "variables": {"nodeId": str(station).upper()},
            },
            headers={"anti-csrftoken-a2z": token},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise StemClientError(f"graphql request failed: {exc}") from exc

    if resp.status_code in (401, 403):
        raise StemAuthError(f"graphql status {resp.status_code}")
    if resp.status_code != 200:
        raise StemClientError(f"graphql status {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise StemClientError("graphql returned non-JSON") from exc

    if payload.get("errors"):
        first = payload["errors"][0] or {}
        raise StemClientError(f"graphql error: {str(first.get('message', ''))[:200]}")

    resources = (payload.get("data") or {}).get("resources")
    if not isinstance(resources, list):
        raise StemClientError("graphql payload missing data.resources")
    return resources


def probe_session(timeout: float = 10.0) -> bool:
    """Cheap liveness check: can we obtain a csrfToken with the cookies?"""
    try:
        _fetch_csrf_token(_session(), timeout)
        return True
    except StemClientError:
        return False
