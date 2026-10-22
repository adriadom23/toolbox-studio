"""Shared client for the Last-Mile *planning* SPAs.

Amazon's Last-Mile planning surface is a family of single-page apps that
all sit behind an API Gateway stage and share the exact same auth
handshake:

    eu.dispatch.planning.last-mile.a2z.com   (Route Sheets, Wave Plan)
        -> https://0vxi8gngy9.execute-api.eu-west-1.amazonaws.com/ws
    eu.assignment.planning.last-mile.a2z.com (DA Assignment)
        -> https://c0xg126vd3.execute-api.eu-west-1.amazonaws.com/web-service
    eu.route.planning.last-mile.a2z.com      (Plan Manifest)
        -> https://wqhfwnv4ee.execute-api.eu-west-1.amazonaws.com/ws

Auth is a two-step session handshake (no SigV4, no Selenium):

1. ``POST /login`` advertises a Midway authorization URL whose ``nonce``
   the server binds to the request. We parse ``client_id`` /
   ``redirect_uri`` / ``nonce`` from it.
2. We mint a Midway **id_token** with that exact nonce (reading the
   Midway session cookies from the user's Firefox profile).
3. ``POST /setsession`` (header ``Authorization: <id_token>``) returns a
   short-lived ``session-token`` (HS256 JWT, ``aud=planning.last-mile.a2z.com``,
   ~12 h). Every ``/api/*`` call is then sent with
   ``Cookie: session-token=<token>``.

Each app caches its own session-token until shortly before expiry.

The planning *program* a route belongs to is identified by a
``cycleId`` UUID, which depends on the **station** and the **cycle**
(CYCLE_1, RTS_2, ...). ``/api/routingConfig/{station}`` exposes the
``cycleName -> cycleId`` mapping, so :func:`resolve_cycle_id` turns the
human cycle selected in the Plan Manifest panel into the UUID the data
endpoints expect.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse

import requests

from app.integrations.firefox_cookies import midway_cookies_from_profile


MIDWAY_SSO_URL = "https://midway-auth.amazon.com/SSO"
DEFAULT_TIMEOUT = 30.0


# --- App registry ----------------------------------------------------------


@dataclass(frozen=True)
class PlanningApp:
    """Configuration for one Last-Mile planning SPA + its API stage."""

    name: str
    site: str  # e.g. https://eu.dispatch.planning.last-mile.a2z.com
    api_base: str  # e.g. https://0vxi8gngy9.execute-api.eu-west-1.amazonaws.com/ws
    enable_lambda_edge: bool

    @property
    def login_url(self) -> str:
        flag = "true" if self.enable_lambda_edge else "false"
        return f"{self.api_base}/login?enableLambdaEdge={flag}"

    @property
    def setsession_url(self) -> str:
        flag = "true" if self.enable_lambda_edge else "false"
        return f"{self.api_base}/setsession?enableLambdaEdge={flag}"

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
                "Gecko/20100101 Firefox/140.0"
            ),
            "Origin": self.site,
            "Referer": self.site + "/",
        }


DISPATCH_APP = PlanningApp(
    name="dispatch",
    site="https://eu.dispatch.planning.last-mile.a2z.com",
    api_base="https://0vxi8gngy9.execute-api.eu-west-1.amazonaws.com/ws",
    enable_lambda_edge=True,
)

ASSIGNMENT_APP = PlanningApp(
    name="assignment",
    site="https://eu.assignment.planning.last-mile.a2z.com",
    api_base="https://c0xg126vd3.execute-api.eu-west-1.amazonaws.com/web-service",
    enable_lambda_edge=False,
)

ROUTE_PLANNING_APP = PlanningApp(
    name="route_planning",
    site="https://eu.route.planning.last-mile.a2z.com",
    api_base="https://wqhfwnv4ee.execute-api.eu-west-1.amazonaws.com/ws",
    enable_lambda_edge=True,
)

SORT_PLANNING_APP = PlanningApp(
    name="sort_planning",
    site="https://eu.sort.planning.last-mile.a2z.com",
    api_base="https://ppj1bqrabh.execute-api.eu-west-1.amazonaws.com/ws",
    enable_lambda_edge=True,
)


# --- Errors ----------------------------------------------------------------


class PlanningError(RuntimeError):
    """Base class for Last-Mile planning errors."""


class PlanningAuthError(PlanningError):
    """No usable Midway session (cannot mint a token / 401/403)."""


class PlanningNetworkError(PlanningError):
    """Connectivity / TLS / timeout issues."""


class PlanningRequestError(PlanningError):
    """The API answered with a non-2xx status or an unexpected body."""


# --- Token plumbing --------------------------------------------------------


# Per-app cache of the exchanged session-token: {app_name: {token, exp}}.
_SESSION_CACHE: dict[str, dict[str, Any]] = {}


def _jwt_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "ignore"))
        return int(data.get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _midway_cookies() -> dict[str, str]:
    try:
        return midway_cookies_from_profile() or {}
    except Exception:  # noqa: BLE001
        return {}


def _begin_login(app: PlanningApp, session: requests.Session, *, return_path: str, timeout: float) -> dict[str, str]:
    """Run ``POST /login`` and return the Midway auth params.

    Returns ``{client_id, redirect_uri, response_type, scope, nonce}``
    parsed from the advertised Midway URL. The anonymous session cookie
    (the server binds the ``nonce`` to it) accumulates on ``session``.
    """
    headers = app.headers()
    headers["Content-Type"] = "application/json"
    body = json.dumps({"returnPath": return_path})
    try:
        response = session.post(
            app.login_url, headers=headers, data=body, timeout=timeout, allow_redirects=False
        )
    except requests.exceptions.Timeout as exc:
        raise PlanningNetworkError(f"{app.name}: login no respondio a tiempo. Revisa la VPN.") from exc
    except requests.exceptions.SSLError as exc:
        raise PlanningNetworkError(f"{app.name}: error SSL en login: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise PlanningNetworkError(
            f"{app.name}: no se pudo conectar (login). Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise PlanningNetworkError(f"{app.name}: error de red en login: {exc}") from exc

    if response.status_code != 200:
        raise PlanningRequestError(
            f"{app.name}: /login devolvio HTTP {response.status_code}: {response.text[:200]}"
        )

    raw = (response.text or "").strip().strip('"')
    if "midway-auth" not in raw or "client_id" not in raw:
        raise PlanningRequestError(f"{app.name}: /login no devolvio una URL de Midway: {raw[:200]}")
    qs = dict(parse_qsl(urlparse(raw).query, keep_blank_values=True))
    if not qs.get("nonce") or not qs.get("client_id"):
        raise PlanningRequestError(f"{app.name}: /login no incluyo nonce/client_id.")
    return qs


def _mint_id_token_for(auth_params: dict[str, str], *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Mint a Midway id_token using the exact params from ``/login``."""
    cookies = _midway_cookies()
    if not cookies.get("session"):
        raise PlanningAuthError(
            "No hay sesion de Midway en Firefox. Abre cualquier pagina interna "
            "e inicia sesion Midway, luego reintenta."
        )

    params = {
        "client_id": auth_params["client_id"],
        "redirect_uri": auth_params.get("redirect_uri") or "",
        "response_type": auth_params.get("response_type") or "id_token",
        "scope": auth_params.get("scope") or "openid",
        "nonce": auth_params["nonce"],
    }
    try:
        response = requests.get(
            MIDWAY_SSO_URL,
            params=params,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
                    "Gecko/20100101 Firefox/140.0"
                ),
                "Accept": "*/*",
            },
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.Timeout as exc:
        raise PlanningNetworkError("Midway no respondio a tiempo. Revisa la VPN.") from exc
    except requests.exceptions.SSLError as exc:
        raise PlanningNetworkError(f"Error SSL con Midway: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise PlanningNetworkError(
            f"No se pudo conectar a Midway. Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise PlanningNetworkError(f"Error de red contra Midway: {exc}") from exc

    body = (response.text or "").strip()
    if response.status_code != 200 or not body.startswith("eyJ"):
        raise PlanningAuthError(
            "Midway no devolvio un token (sesion expirada). Recarga una pagina "
            f"Midway en Firefox y reintenta. (HTTP {response.status_code})"
        )
    return body


def get_session_token(
    app: PlanningApp, *, return_path: str = "/", force: bool = False, timeout: float = DEFAULT_TIMEOUT
) -> str:
    """Return a valid session-token for ``app``, running the handshake on demand."""
    now = int(time.time())
    cache = _SESSION_CACHE.setdefault(app.name, {"token": "", "exp": 0})
    if not force and cache.get("token") and cache.get("exp", 0) - 120 > now:
        return cache["token"]

    session = requests.Session()
    auth_params = _begin_login(app, session, return_path=return_path, timeout=timeout)
    id_token = _mint_id_token_for(auth_params, timeout=timeout)

    headers = app.headers()
    headers["Authorization"] = id_token
    headers["Content-Type"] = "application/json"
    try:
        response = session.post(
            app.setsession_url, headers=headers, data="{}", timeout=timeout, allow_redirects=False
        )
    except requests.exceptions.Timeout as exc:
        raise PlanningNetworkError(f"{app.name}: setsession no respondio a tiempo. Revisa la VPN.") from exc
    except requests.exceptions.SSLError as exc:
        raise PlanningNetworkError(f"{app.name}: error SSL en setsession: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise PlanningNetworkError(
            f"{app.name}: no se pudo conectar (setsession). Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise PlanningNetworkError(f"{app.name}: error de red en setsession: {exc}") from exc

    if response.status_code in (401, 403):
        raise PlanningAuthError(
            f"{app.name}: rechazo el id_token ({response.status_code}). Recarga Midway en Firefox."
        )
    if response.status_code != 200:
        raise PlanningRequestError(
            f"{app.name}: setsession devolvio HTTP {response.status_code}: {response.text[:200]}"
        )

    token = (response.text or "").strip().strip('"')
    if not token.startswith("eyJ"):
        token = session.cookies.get("session-token", "") or ""
    if not token.startswith("eyJ"):
        raise PlanningRequestError(f"{app.name}: setsession no devolvio un session-token valido.")

    cache["token"] = token
    cache["exp"] = _jwt_exp(token) or (now + 600)
    return token


def get_json(
    app: PlanningApp,
    path: str,
    *,
    return_path: str = "/",
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """GET ``{api_base}{path}`` authenticated with the app session-token.

    Re-runs the handshake once if the token is rejected. Returns the
    decoded JSON.
    """
    url = f"{app.api_base}{path}"

    def _do(token: str) -> requests.Response:
        return requests.get(
            url,
            headers=app.headers(),
            cookies={"session-token": token},
            timeout=timeout,
            allow_redirects=False,
        )

    token = get_session_token(app, return_path=return_path, timeout=timeout)
    try:
        response = _do(token)
    except requests.exceptions.Timeout as exc:
        raise PlanningNetworkError(f"{app.name}: no respondio en {timeout:.0f}s. Revisa la VPN.") from exc
    except requests.exceptions.SSLError as exc:
        raise PlanningNetworkError(f"{app.name}: error SSL: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise PlanningNetworkError(
            f"{app.name}: no se pudo conectar. Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise PlanningNetworkError(f"{app.name}: error de red: {exc}") from exc

    if response.status_code in (401, 403):
        token = get_session_token(app, return_path=return_path, force=True, timeout=timeout)
        response = _do(token)
        if response.status_code in (401, 403):
            raise PlanningAuthError(
                f"{app.name}: rechazo la sesion ({response.status_code}). Recarga Midway en Firefox."
            )

    if response.status_code != 200:
        raise PlanningRequestError(f"{app.name}: HTTP {response.status_code}: {response.text[:200]}")

    try:
        return response.json()
    except ValueError as exc:
        raise PlanningRequestError(f"{app.name}: respuesta no-JSON.") from exc


def get_text(
    app: PlanningApp,
    path: str,
    *,
    return_path: str = "/",
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """GET ``{api_base}{path}`` authenticated with the app session-token.

    Like :func:`get_json` but returns the raw response body as text
    (with any surrounding quotes stripped). Used by endpoints that hand
    back a bare value such as a presigned S3 URL (e.g. ``dayOfOpsPlan``).
    """
    url = f"{app.api_base}{path}"

    def _do(token: str) -> requests.Response:
        return requests.get(
            url,
            headers=app.headers(),
            cookies={"session-token": token},
            timeout=timeout,
            allow_redirects=False,
        )

    token = get_session_token(app, return_path=return_path, timeout=timeout)
    try:
        response = _do(token)
    except requests.exceptions.Timeout as exc:
        raise PlanningNetworkError(f"{app.name}: no respondio en {timeout:.0f}s. Revisa la VPN.") from exc
    except requests.exceptions.SSLError as exc:
        raise PlanningNetworkError(f"{app.name}: error SSL: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise PlanningNetworkError(
            f"{app.name}: no se pudo conectar. Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise PlanningNetworkError(f"{app.name}: error de red: {exc}") from exc

    if response.status_code in (401, 403):
        token = get_session_token(app, return_path=return_path, force=True, timeout=timeout)
        response = _do(token)
        if response.status_code in (401, 403):
            raise PlanningAuthError(
                f"{app.name}: rechazo la sesion ({response.status_code}). Recarga Midway en Firefox."
            )

    if response.status_code != 200:
        raise PlanningRequestError(f"{app.name}: HTTP {response.status_code}: {response.text[:200]}")

    return (response.text or "").strip().strip('"')


def login_status(app: PlanningApp) -> dict[str, Any]:
    """Report whether the Midway session can mint a session token for ``app``."""
    cookies = _midway_cookies()
    if not cookies.get("session"):
        return {"ready": False, "reason": "no_midway_session", "login_url": app.site}
    try:
        token = get_session_token(app)
    except PlanningError as exc:
        return {"ready": False, "reason": str(exc), "login_url": app.site}
    return {"ready": bool(token), "login_url": app.site}


# --- Helpers ---------------------------------------------------------------


def normalize_station(station: str) -> str:
    code = (station or "").strip().upper()
    if not code or not code.isalnum() or len(code) > 6:
        raise ValueError(f"Station code invalido: {station!r}")
    return code


def normalize_date(plan_date: date | datetime | str | None) -> str:
    if plan_date is None:
        return date.today().isoformat()
    if isinstance(plan_date, datetime):
        return plan_date.date().isoformat()
    if isinstance(plan_date, date):
        return plan_date.isoformat()
    text = str(plan_date).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"date no parseable: {plan_date!r}")


def normalize_cycle_name(cycle: str | None) -> str:
    """Canonicalise a cycle label to the ``CYCLE_1`` / ``RTS_2`` form."""
    return (cycle or "").strip().upper().replace(" ", "_").replace("-", "_")


def list_supported_cycles(
    app: PlanningApp, station: str, plan_date: date | datetime | str | None = None, *, timeout: float = DEFAULT_TIMEOUT
) -> list[dict[str, Any]]:
    """Return ``supportedCycles`` for ``station`` from ``/api/routingConfig``."""
    code = normalize_station(station)
    date_iso = normalize_date(plan_date)
    path = f"/api/routingConfig/{code}?planDate={date_iso}"
    data = get_json(app, path, return_path=f"/{code}", timeout=timeout)
    if isinstance(data, dict):
        cycles = data.get("supportedCycles")
        if isinstance(cycles, list):
            return cycles
    return []


def resolve_cycle_id(
    app: PlanningApp,
    station: str,
    cycle: str,
    plan_date: date | datetime | str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Resolve a cycle name (e.g. ``CYCLE_1``) to its ``cycleId`` UUID.

    Raises :class:`ValueError` if the cycle is not available for the
    given station/date.
    """
    wanted = normalize_cycle_name(cycle)
    if not wanted:
        raise ValueError("Cycle vacio.")
    cycles = list_supported_cycles(app, station, plan_date, timeout=timeout)
    for entry in cycles:
        if normalize_cycle_name(str(entry.get("cycleName") or "")) == wanted:
            cid = str(entry.get("cycleId") or "").strip()
            if cid:
                return cid
    available = ", ".join(
        sorted({str(c.get("cycleName") or "") for c in cycles if c.get("cycleName")})
    )
    raise ValueError(
        f"Cycle '{cycle}' no disponible para {normalize_station(station)}. "
        f"Disponibles: {available or '(ninguno)'}"
    )


__all__ = [
    "ASSIGNMENT_APP",
    "DISPATCH_APP",
    "ROUTE_PLANNING_APP",
    "SORT_PLANNING_APP",
    "DEFAULT_TIMEOUT",
    "PlanningApp",
    "PlanningAuthError",
    "PlanningError",
    "PlanningNetworkError",
    "PlanningRequestError",
    "get_json",
    "get_session_token",
    "get_text",
    "list_supported_cycles",
    "login_status",
    "normalize_cycle_name",
    "normalize_date",
    "normalize_station",
    "resolve_cycle_id",
]
