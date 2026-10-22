"""Amazon Relay tracker (``track.relay.amazon.dev``) client.

The Relay trip / vehicleRun tracker is a SPA whose list endpoint
``/api/v2/transport-views`` is fronted by API Gateway + CloudFront and
authenticates with a Midway **Bearer id_token** whose audience is
``track.relay.amazon.dev``. The ``amzn_sso_token`` cookie does NOT work
here (its audience is ``...:443``), so — unlike Mercury/Siphon which
accept the SSO cookie directly — we have to mint the right token.

We mint it exactly like the browser does, with no Selenium:

    GET https://midway-auth.amazon.com/SSO
        ?client_id=track.relay.amazon.dev
        &redirect_uri=https://track.relay.amazon.dev/
        &response_type=id_token&scope=openid&nonce=<random>

sent with the Midway session cookies read from the user's Firefox
profile. Midway returns the id_token in the (text/plain) body. The
token lives ~15 min, so we cache it in-memory until shortly before it
expires and re-mint on demand.

Public API:

* :func:`relay_login_status` — reports whether the Midway session can
  mint a token.
* :func:`fetch_vehicle_runs` — mints a token and returns the raw
  vehicle-run records for a node/site.

Errors: :class:`RelayAuthError`, :class:`RelayNetworkError`,
:class:`RelayRequestError`.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import date, timedelta
from typing import Any

import requests

from app.integrations.firefox_cookies import (
    midway_cookies_from_profile,
    relay_cookies_from_profile,
)


RELAY_SITE = "https://track.relay.amazon.dev"
RELAY_TRANSPORT_VIEWS_URL = f"{RELAY_SITE}/api/v2/transport-views"
MIDWAY_SSO_URL = "https://midway-auth.amazon.com/SSO"
RELAY_CLIENT_ID = "track.relay.amazon.dev"
RELAY_REDIRECT_URI = "https://track.relay.amazon.dev/"
RELAY_DEFAULT_TIMEOUT = 30.0

RELAY_VR_STATUSES: tuple[str, ...] = (
    "ARRIVED_AT_FINAL_DESTINATION",
    "EXECUTION_STARTED",
    "IN_TRANSIT",
    "PLANNED",
)

_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "*/*",
    "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    ),
    "Origin": RELAY_SITE,
    "Referer": RELAY_SITE + "/",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RelayError(RuntimeError):
    """Base class for Relay errors."""


class RelayAuthError(RelayError):
    """No usable Midway session (cannot mint a token / 401)."""


class RelayNetworkError(RelayError):
    """Connectivity / TLS / timeout issues."""


class RelayRequestError(RelayError):
    """Relay answered with a non-2xx status."""


# ---------------------------------------------------------------------------
# Token minting (Midway OIDC)
# ---------------------------------------------------------------------------


_TOKEN_CACHE: dict[str, Any] = {"token": "", "exp": 0}


def _jwt_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "ignore"))
        return int(data.get("exp") or 0)
    except Exception:
        return 0


def _midway_cookies() -> dict[str, str]:
    try:
        return midway_cookies_from_profile() or {}
    except Exception:
        return {}


def mint_id_token(*, force: bool = False, timeout: float = RELAY_DEFAULT_TIMEOUT) -> str:
    """Mint (or reuse a cached) Midway id_token for the Relay audience.

    Raises :class:`RelayAuthError` when the Midway session is missing or
    expired so the caller can tell the operator to re-open Midway.
    """
    now = int(time.time())
    cached = _TOKEN_CACHE.get("token") or ""
    if not force and cached and _TOKEN_CACHE.get("exp", 0) - 60 > now:
        return cached

    cookies = _midway_cookies()
    if not cookies.get("session"):
        raise RelayAuthError(
            "No hay sesion de Midway en Firefox. Abre cualquier pagina interna "
            "(o track.relay.amazon.dev) e inicia sesion Midway, luego reintenta."
        )

    params = {
        "client_id": RELAY_CLIENT_ID,
        "redirect_uri": RELAY_REDIRECT_URI,
        "response_type": "id_token",
        "scope": "openid",
        "nonce": secrets.token_hex(24),
    }
    try:
        response = requests.get(
            MIDWAY_SSO_URL,
            params=params,
            headers={"User-Agent": _DEFAULT_HEADERS["User-Agent"], "Accept": "*/*"},
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.Timeout as exc:
        raise RelayNetworkError("Midway no respondio a tiempo. Revisa la VPN.") from exc
    except requests.exceptions.SSLError as exc:
        raise RelayNetworkError(f"Error SSL con Midway: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise RelayNetworkError(
            f"No se pudo conectar a Midway. Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RelayNetworkError(f"Error de red contra Midway: {exc}") from exc

    body = (response.text or "").strip()
    if response.status_code != 200 or not body.startswith("eyJ"):
        raise RelayAuthError(
            "Midway no devolvio un token (sesion expirada). Recarga una pagina "
            f"Midway en Firefox y reintenta. (HTTP {response.status_code})"
        )

    _TOKEN_CACHE["token"] = body
    _TOKEN_CACHE["exp"] = _jwt_exp(body) or (now + 600)
    return body


def relay_login_status() -> dict[str, Any]:
    """Report whether the Midway session can mint a Relay token."""
    cookies = _midway_cookies()
    if not cookies.get("session"):
        return {"ready": False, "reason": "no_midway_session", "login_url": RELAY_SITE}
    try:
        token = mint_id_token()
    except RelayError as exc:
        return {"ready": False, "reason": str(exc), "login_url": RELAY_SITE}
    return {"ready": bool(token), "login_url": RELAY_SITE}


# ---------------------------------------------------------------------------
# Vehicle-run list
# ---------------------------------------------------------------------------


def fetch_vehicle_runs(
    *,
    station: str,
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int = 200,
    timeout: float = RELAY_DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the vehicle-run records for ``station`` from Relay.

    Mints a Midway token, calls ``transport-views`` and returns the list
    of raw record dicts (mapped to the camiones frame by
    :func:`app.tools.common.camiones.parse_camiones_json`).
    """
    if not station:
        raise ValueError("station is required.")

    token = mint_id_token(timeout=timeout)

    today = date.today()
    start = start_date or (today - timedelta(days=1)).isoformat()
    end = end_date or (today + timedelta(days=1)).isoformat()

    params: list[tuple[str, Any]] = [
        ("type[]", "vehicleRun"),
        ("module", "trip"),
        ("page", 1),
        ("pageSize", int(page_size)),
    ]
    params += [("status[]", status) for status in RELAY_VR_STATUSES]
    params += [
        ("sortCol", "sent"),
        ("ascending", "true"),
        ("startDate", start),
        ("endDate", end),
        ("startTime", "00:00:00"),
        ("endTime", "23:59:59"),
        ("column", "scheduled_end"),
        ("view", "detail"),
        ("dynamicSearchFields", json.dumps({"nodeCodes": [str(station).upper()]})),
        ("dateField", "effectiveEnd"),
    ]

    headers = dict(_DEFAULT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"

    relay_ck = {}
    try:
        relay_ck = {
            k: v
            for k, v in (relay_cookies_from_profile() or {}).items()
            if k in ("amzn_sso_token", "amzn_sso_rfp")
        }
    except Exception:
        relay_ck = {}

    try:
        response = requests.get(
            RELAY_TRANSPORT_VIEWS_URL,
            params=params,
            headers=headers,
            cookies=relay_ck,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.Timeout as exc:
        raise RelayNetworkError(f"Relay no respondio en {timeout:.0f}s. Revisa la VPN.") from exc
    except requests.exceptions.SSLError as exc:
        raise RelayNetworkError(f"Error SSL con Relay: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise RelayNetworkError(
            f"No se pudo conectar a Relay. Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RelayNetworkError(f"Error de red contra Relay: {exc}") from exc

    if response.status_code in (401, 403):
        # Token might have just expired — re-mint once and retry.
        token = mint_id_token(force=True, timeout=timeout)
        headers["Authorization"] = f"Bearer {token}"
        response = requests.get(
            RELAY_TRANSPORT_VIEWS_URL,
            params=params,
            headers=headers,
            cookies=relay_ck,
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise RelayAuthError(
                f"Relay rechazo el token ({response.status_code}). Recarga Midway en Firefox."
            )

    if response.status_code != 200:
        raise RelayRequestError(f"Relay HTTP {response.status_code}: {response.text[:200]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise RelayRequestError("Relay devolvio una respuesta no-JSON.") from exc

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


__all__ = [
    "RELAY_SITE",
    "RELAY_TRANSPORT_VIEWS_URL",
    "RelayAuthError",
    "RelayError",
    "RelayNetworkError",
    "RelayRequestError",
    "fetch_vehicle_runs",
    "mint_id_token",
    "relay_login_status",
]
