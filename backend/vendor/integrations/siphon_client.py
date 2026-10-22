"""Siphon (siphon-monolith / SUI) integration helpers.

Siphon's ``/api/sui`` endpoint returns the **DSP SUI Capacity** view
that powers the Forecast +3Wks (Metis) tool — the same JSON shown in
the network panel:

```
{"CYCLE_1": [{"dsp_name": "CDOR", "date": "2026-06-07",
              "input_route_count": 27, "service_types_data": {...}}, ...]}
```

The legacy ``MetisPlan.py`` opened Firefox + Selenium to capture the
session cookie and then fired SigV4-signed requests against
``execute-api`` (okami-routes). We can skip all that because once the
operator has logged into Midway the relevant cookies live in their
Firefox profile — same approach Mercury / DINO Harmony / PerfectMile
already use.

Public API:

* :func:`siphon_login_status` — checks the Firefox cookies and reports
  if the session is usable.
* :func:`fetch_sui_week` — pulls a single week (returns the parsed
  rows + the original JSON).
* :func:`fetch_sui_weeks` — convenience wrapper that fetches the
  three "next 3 weeks" relative to today (or any anchor) and merges
  their rows into a single list.

Errors:

* :class:`SiphonAuthError` — cookies missing / token expired.
* :class:`SiphonNetworkError` — DNS / TLS / connect issues.
* :class:`SiphonRequestError` — Siphon returned a non-2xx HTTP code.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any

import requests

from app.integrations.firefox_cookies import (
    SIPHON_HOST_PATTERNS,
    SIPHON_REQUIRED_COOKIES,
    has_required,
    read_cookies_from_profile,
)


SIPHON_HOST = "siphon-monolith.corp.amazon.com"
# Host of the SPA the operator normally browses. Midway mints
# ``amzn_sso_token`` host-only PER HOST, so a login on the SPA does not
# refresh the token of the API host (and vice versa).
SIPHON_SPA_HOST = "siphon.amazon.dev"
# The login link must point at the host whose cookie we forward to
# ``/api/sui`` — opening it top-level runs the Midway redirect dance and
# mints the host-only ``amzn_sso_token`` exactly where we read it.
# (The old ``https://siphon.amazon.dev/ci_entry`` link minted the token
# on the SPA host only, which the API-host-scoped reader ignored — the
# operator ended up clearing cookies to force a full re-auth.)
SIPHON_LOGIN_URL = f"https://{SIPHON_HOST}/"
SIPHON_SUI_URL = f"https://{SIPHON_HOST}/api/sui"
SIPHON_DEFAULT_TIMEOUT = 20.0
SIPHON_DEFAULT_REGION = "eu"


# Headers that mirror a real Firefox request.
_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,es-ES;q=0.8,es;q=0.5,en;q=0.3",
    "Connection": "keep-alive",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    ),
    "Referer": "https://siphon.amazon.dev/",
    "Origin": "https://siphon.amazon.dev",
}


# Allow-list of cookies forwarded to Siphon. The browser-trace the
# operator shared shows ``user``, ``realm``, ``domain``,
# ``enable_midway`` and the central Amazon SSO cookies. Anything else
# (tracking / marketing) is dropped.
_COOKIE_WHITELIST: tuple[str, ...] = (
    "user",
    "realm",
    "domain",
    "enable_midway",
    "amzn_sso_token",
    "amzn_sso_rfp",
    "session-id",
    "session-id-time",
    "session-token",
    "sess-at-main",
    "at-main",
    "x-main",
    "ubid-main",
    "JSESSIONID",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SiphonError(RuntimeError):
    """Base class for Siphon errors."""


class SiphonAuthError(SiphonError):
    """Raised when the user has no usable Siphon session."""


class SiphonNetworkError(SiphonError):
    """Connectivity / TLS / timeout issues."""


class SiphonRequestError(SiphonError):
    """Siphon answered with a non-2xx status."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_cookies_for(target_host: str) -> dict[str, str]:
    try:
        return read_cookies_from_profile(
            SIPHON_HOST_PATTERNS,
            target_host=target_host,
        )
    except Exception:
        return {}


def _read_cookies() -> dict[str, str]:
    """Read the Siphon cookies from Firefox, API host first.

    Midway mints ``amzn_sso_token`` host-only per host, so we prefer the
    token bound to the API host (``SIPHON_HOST``). When that one is
    missing or expired we fall back to the SPA host token — the operator
    usually logs in there, and a fresh JWT beats a stale one. This keeps
    a valid session detectable without asking the operator to clear
    cookies.
    """
    primary = _read_cookies_for(SIPHON_HOST)
    token = primary.get("amzn_sso_token", "")
    if token and not _sso_token_expired(token):
        return primary

    fallback = _read_cookies_for(SIPHON_SPA_HOST)
    fb_token = fallback.get("amzn_sso_token", "")
    if fb_token and not _sso_token_expired(fb_token):
        return fallback

    return primary or fallback


def _filter_cookies(raw: dict[str, str]) -> dict[str, str]:
    if not raw:
        return {}
    return {name: value for name, value in raw.items() if name in _COOKIE_WHITELIST}


def _sso_token_expired(token: str) -> bool:
    """Cheap JWT exp parser. ``True`` when the token has expired."""
    import base64
    import json
    import time

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return False
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8", errors="ignore"))
        exp = int(payload.get("exp") or 0)
        if exp <= 0:
            return False
        return exp <= int(time.time())
    except Exception:
        return False


def siphon_login_status() -> dict[str, Any]:
    """Mirror :func:`mercury_login_status` for the Siphon tool."""
    cookies = _read_cookies()
    if not cookies:
        return {
            "ready": False,
            "missing": list(SIPHON_REQUIRED_COOKIES),
            "cookie_count": 0,
            "token_expired": False,
        }
    missing = [name for name in SIPHON_REQUIRED_COOKIES if name not in cookies]
    token_expired = False
    token = cookies.get("amzn_sso_token", "")
    if token:
        token_expired = _sso_token_expired(token)
    ready = (not missing) and (not token_expired)
    return {
        "ready": ready,
        "missing": missing,
        "cookie_count": len(cookies),
        "token_expired": token_expired,
        "user": cookies.get("user", ""),
    }


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def amazon_week_for(date: _dt.date | None = None) -> tuple[int, int]:
    """Return ``(year, week)`` using ISO week numbers.

    Amazon weeks track ISO weeks closely enough for the Siphon SUI
    endpoint, which expects ``year`` and ``week`` as integers.
    """
    target = date or _dt.date.today()
    iso_year, iso_week, _ = target.isocalendar()
    return int(iso_year), int(iso_week)


def next_three_weeks(anchor: _dt.date | None = None) -> list[tuple[int, int]]:
    """Return the (year, week) tuples for ``anchor`` +1 / +2 / +3 weeks."""
    base = anchor or _dt.date.today()
    out: list[tuple[int, int]] = []
    for offset in (1, 2, 3):
        future = base + _dt.timedelta(weeks=offset)
        out.append(amazon_week_for(future))
    return out


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_sui_week(
    *,
    station: str,
    year: int,
    week: int,
    region: str | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = SIPHON_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> dict[str, Any]:
    """Fetch one Amazon week of SUI data.

    Returns the raw JSON dict (``{"CYCLE_1": [{...}], ...}``) so the
    caller can choose which cycles to consume.
    """
    if not station:
        raise ValueError("station is required.")

    session_cookies = cookies if cookies is not None else _read_cookies()
    if not has_required(session_cookies, SIPHON_REQUIRED_COOKIES):
        raise SiphonAuthError(
            "No hay sesion de Siphon valida. Abre "
            f"{SIPHON_LOGIN_URL} en Firefox (Midway se encarga del login) "
            "y vuelve a intentarlo. No hace falta limpiar cookies."
        )
    session_cookies = _filter_cookies(session_cookies)
    if not has_required(session_cookies, SIPHON_REQUIRED_COOKIES):
        raise SiphonAuthError(
            "Cookies Siphon filtradas pero faltan las requeridas. Vuelve a iniciar sesion."
        )

    if verify is None:
        env_verify = os.environ.get("SIPHON_VERIFY_TLS", "").strip().lower()
        if env_verify in {"0", "false", "no", "off"}:
            verify = False
        else:
            verify = True

    params = {
        "region": (region or SIPHON_DEFAULT_REGION).lower(),
        "station": station.upper(),
        "week": int(week),
        "year": int(year),
    }
    headers = dict(_DEFAULT_HEADERS)

    try:
        response = requests.get(
            SIPHON_SUI_URL,
            params=params,
            headers=headers,
            cookies=session_cookies,
            timeout=timeout,
            allow_redirects=False,
            verify=verify,
        )
    except requests.exceptions.Timeout as exc:
        raise SiphonNetworkError(
            f"Siphon no respondio en {timeout:.0f}s. Revisa la VPN."
        ) from exc
    except requests.exceptions.SSLError as exc:
        raise SiphonNetworkError(
            "Error SSL con Siphon (proxy corporativo). "
            "Confirma el root cert o desactiva la verificacion con SIPHON_VERIFY_TLS=0. "
            f"Detalle: {exc}"
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise SiphonNetworkError(
            f"No se pudo conectar a {SIPHON_HOST}. Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise SiphonNetworkError(f"Error de red contra {SIPHON_HOST}: {exc}") from exc

    if response.status_code in (301, 302, 303, 307, 308):
        # Redirects to the Midway login page = expired session.
        raise SiphonAuthError(
            "Siphon redirigio a Midway. La sesion expiro — abre "
            f"{SIPHON_LOGIN_URL} en Firefox y vuelve a intentarlo."
        )
    if response.status_code == 401 or response.status_code == 403:
        raise SiphonAuthError(
            f"Siphon respondio {response.status_code}. Vuelve a iniciar sesion."
        )
    if response.status_code != 200:
        raise SiphonRequestError(
            f"Siphon HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise SiphonRequestError(
            f"Respuesta no-JSON de Siphon: {response.text[:200]}"
        ) from exc


def fetch_sui_weeks(
    *,
    station: str,
    weeks: list[tuple[int, int]] | None = None,
    region: str | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = SIPHON_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch several weeks at once. Returns ``[{year, week, payload}, ...]``."""
    target_weeks = weeks or next_three_weeks()
    out: list[dict[str, Any]] = []
    cookie_cache = cookies
    for year, week in target_weeks:
        data = fetch_sui_week(
            station=station,
            year=year,
            week=week,
            region=region,
            cookies=cookie_cache,
            timeout=timeout,
            verify=verify,
        )
        out.append({"year": year, "week": week, "payload": data})
    return out


def flatten_sui_payload(
    payload: dict[str, Any],
    *,
    cycles: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten the SUI payload into a list of ``{dsp, date, routes, cycle}``.

    By default only ``CYCLE_1`` is consumed (matches the legacy
    ``MetisPlan.py`` behaviour and what the operator showed in the
    network trace). Pass an explicit ``cycles`` list to include more.
    """
    if not isinstance(payload, dict):
        return []
    target_cycles = list(cycles) if cycles else ["CYCLE_1"]
    out: list[dict[str, Any]] = []
    for cycle_key, items in payload.items():
        if cycle_key not in target_cycles:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            dsp = str(item.get("dsp_name") or "").strip()
            date = str(item.get("date") or "").strip()
            routes = item.get("input_route_count")
            try:
                routes_val = int(routes) if routes is not None else 0
            except (TypeError, ValueError):
                routes_val = 0
            if not dsp or not date:
                continue
            out.append(
                {
                    "dsp": dsp,
                    "date": date,
                    "routes": routes_val,
                    "cycle": cycle_key,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Okami Baseline Routes (v1/okami-routes)
# ---------------------------------------------------------------------------
#
# The Siphon SPA renders an "Okami Baseline Routes" row in its Station
# Summary table (the xpath the operator sees ends in
# ``.../table/tbody/tr[2]/td[1]/span/div``). That number does NOT come from
# ``/api/sui``: the SPA calls ``GET {apiEndpoint}/v1/okami-routes`` on an
# execute-api gateway, SigV4-signed with the temporary AWS credentials the
# monolith hands out at ``/api/se2/auth`` (cookie-authenticated). Flow
# validated against production on 2026-08-15:
#
#   1. GET https://siphon-monolith.corp.amazon.com/api/se2/client-endpoints
#      → {"apiEndpoint": "https://xxxx.execute-api.us-west-2.amazonaws.com"}
#   2. GET https://siphon-monolith.corp.amazon.com/api/se2/auth
#      → {AccessKeyId, SecretAccessKey, SessionToken, Expiration}
#   3. SigV4 GET {apiEndpoint}/v1/okami-routes?region=EU&station=DCZ4&
#      business_type=AMZL&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
#      → {"items": [{date, dsp, cycle, service_type, resource_pool,
#                    offered_routes, tco_routes, standard_routes}], ...}
#
# When the monolith session cookie has expired the ``/api/se2/*`` calls
# answer 307 to Midway; seeding the Midway cookies from the Firefox profile
# and following the redirects re-mints the session transparently (same
# trick as the PerfectMile Caravan client).


def _monolith_session(cookies: dict[str, str] | None = None) -> "requests.Session":
    """Session with the Siphon + Midway cookies from the Firefox profile.

    OJO (fix 2026-08-18): las cookies de Midway se siembran SOLO en
    ``midway-auth.amazon.com``. Sembrarlas también en ``.amazon.com``
    (como hace el cliente de Caravan) infla la cabecera Cookie de las
    peticiones al monolith (~6,7 KB con un perfil normal) y el monolith
    responde **HTTP 431** en ``/api/se2/*`` — eso dejaba el Okami
    Baseline (tablas FRT / Okami baseline del OTRM) siempre "no
    disponible". Para el re-mint basta con que el salto a midway-auth
    lleve su sesión; el monolith solo necesita sus propias cookies.

    ``cookies`` lets a caller forward a jar already read from Firefox so a
    multi-day/weekly fetch reuses ONE jar instead of re-reading the profile
    for every request (Nursery Checker roster + Dive Deep).
    """
    from app.integrations.firefox_cookies import midway_cookies_from_profile

    session = requests.Session()
    session.headers.update(_DEFAULT_HEADERS)
    try:
        for name, value in midway_cookies_from_profile().items():
            session.cookies.set(name, value, domain="midway-auth.amazon.com")
    except Exception:  # noqa: BLE001 — Midway seed is best-effort
        pass
    jar = cookies if cookies is not None else _read_cookies()
    for name, value in _filter_cookies(jar).items():
        session.cookies.set(name, value, domain=SIPHON_HOST)
    return session


def _okami_routes_items(
    *,
    station: str,
    start_date: str,
    end_date: str,
    region: str | None,
    business_type: str,
    cookies: dict[str, str] | None,
    timeout: float,
    verify: bool | str | None,
) -> list[dict[str, Any]]:
    """Run the ``/api/se2/*`` + SigV4 ``/v1/okami-routes`` flow once and return
    the raw ``items`` list untouched (every field preserved).

    Used by :func:`fetch_okami_baseline_map` so the auth dance lives in a
    single place and an optional ``cookies`` override keeps the whole window on
    one jar/session. The okami-routes feed is aggregated by ``(cycle, dsp,
    service_type, date)`` and carries no driver/transporter identity — it is
    NOT a driver-level source.
    """
    if not station:
        raise ValueError("station is required.")
    verify_flag = _verify_flag(verify)
    session = _monolith_session(cookies)

    cfg = _monolith_json(
        session, "/api/se2/client-endpoints", timeout=timeout, verify=verify_flag
    )
    endpoint = str((cfg or {}).get("apiEndpoint") or "").rstrip("/")
    if not endpoint:
        raise SiphonRequestError("Siphon no devolvio el apiEndpoint de se2.")
    creds = _monolith_json(
        session, "/api/se2/auth", timeout=timeout, verify=verify_flag
    )
    if not isinstance(creds, dict) or not creds.get("AccessKeyId"):
        raise SiphonAuthError("Siphon no devolvio credenciales se2 utilizables.")

    response = _sigv4_get(
        f"{endpoint}/v1/okami-routes",
        {
            "region": (region or SIPHON_DEFAULT_REGION).upper(),
            "station": station.upper(),
            "business_type": business_type,
            "start_date": start_date,
            "end_date": end_date,
        },
        creds,
        timeout=max(timeout, 30.0),
        verify=verify_flag,
    )
    if response.status_code in (401, 403):
        raise SiphonAuthError(f"okami-routes respondio {response.status_code}.")
    if response.status_code != 200:
        raise SiphonRequestError(
            f"okami-routes HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise SiphonRequestError("Respuesta no-JSON de okami-routes.") from exc

    items = data.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _verify_flag(verify: bool | str | None) -> bool | str:
    if verify is not None:
        return verify
    env_verify = os.environ.get("SIPHON_VERIFY_TLS", "").strip().lower()
    return env_verify not in {"0", "false", "no", "off"}


def _monolith_json(
    session: "requests.Session",
    path: str,
    *,
    timeout: float,
    verify: bool | str,
) -> Any:
    """GET a cookie-authenticated monolith endpoint, following the Midway
    redirect dance when the session needs re-minting."""
    try:
        response = session.get(
            f"https://{SIPHON_HOST}{path}",
            timeout=timeout,
            allow_redirects=True,
            verify=verify,
        )
    except requests.exceptions.Timeout as exc:
        raise SiphonNetworkError(f"Siphon no respondio en {timeout:.0f}s.") from exc
    except requests.exceptions.RequestException as exc:
        raise SiphonNetworkError(f"Error de red contra {SIPHON_HOST}: {exc}") from exc
    if response.status_code in (401, 403):
        raise SiphonAuthError(f"Siphon respondio {response.status_code} en {path}.")
    if response.status_code != 200:
        raise SiphonRequestError(f"Siphon HTTP {response.status_code} en {path}.")
    try:
        return response.json()
    except ValueError as exc:
        # An HTML body here means we landed on the Midway login page.
        raise SiphonAuthError(
            "La sesion de Siphon expiro y Midway pide login interactivo. "
            f"Abre {SIPHON_LOGIN_URL} en Firefox y vuelve a intentarlo."
        ) from exc


def _sigv4_get(
    url: str,
    params: dict[str, Any],
    creds: dict[str, str],
    *,
    timeout: float,
    verify: bool | str,
) -> "requests.Response":
    """Minimal SigV4 (query-in-canonical, empty payload) GET."""
    import datetime as _dtm
    import hashlib
    import hmac
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    # execute-api host: <id>.execute-api.<region>.amazonaws.com
    parts = host.split(".")
    service = parts[1]
    region = parts[2]

    qs = "&".join(
        f"{urllib.parse.quote(str(k), safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
        for k, v in sorted(params.items())
    )
    now = _dtm.datetime.now(_dtm.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "x-amz-security-token": creds["SessionToken"],
    }
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        ["GET", parsed.path, qs, canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = _hmac(("AWS4" + creds["SecretAccessKey"]).encode(), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return requests.get(
        f"{url}?{qs}",
        headers={
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={creds['AccessKeyId']}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "x-amz-date": amz_date,
            "x-amz-security-token": creds["SessionToken"],
            "x-amz-content-sha256": payload_hash,
            "Accept": "application/json",
        },
        timeout=timeout,
        verify=verify,
    )


def fetch_okami_baseline_map(
    *,
    station: str,
    start_date: str,
    end_date: str,
    region: str | None = None,
    business_type: str = "AMZL",
    cycles: list[str] | None = None,
    timeout: float = SIPHON_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> dict[tuple[str, str], int]:
    """Okami Baseline Routes per ``(dsp_short, ISO date)``.

    Sums ``offered_routes`` over the service types of each DSP + day
    (``CYCLE_1`` by default — matches the SUI consumption and the Station
    Summary row the operator reads in the Siphon UI). Raises the standard
    Siphon errors on auth/network problems.
    """
    items = _okami_routes_items(
        station=station,
        start_date=start_date,
        end_date=end_date,
        region=region,
        business_type=business_type,
        cookies=None,
        timeout=timeout,
        verify=verify,
    )

    target_cycles = set(cycles) if cycles else {"CYCLE_1"}
    out: dict[tuple[str, str], int] = {}
    for item in items:
        if str(item.get("cycle") or "") not in target_cycles:
            continue
        dsp = str(item.get("dsp") or "").strip().upper()
        date = str(item.get("date") or "").strip()
        if not dsp or not date:
            continue
        try:
            routes = int(item.get("offered_routes") or 0)
        except (TypeError, ValueError):
            routes = 0
        key = (dsp, date)
        out[key] = out.get(key, 0) + routes
    return out


def merge_weeks_to_routes(
    weeks_data: list[dict[str, Any]],
    *,
    cycles: list[str] | None = None,
) -> dict[tuple[str, str], int]:
    """Build the ``{(dsp_short, day): routes}`` map used by Metis Plan.

    DSP shortening matches the operator's expectation — the SUI feed
    already exposes the short codes (``CDOR``, ``DESL``, ``JSIS``…)
    so we just upper-case + strip.
    """
    out: dict[tuple[str, str], int] = {}
    for entry in weeks_data:
        rows = flatten_sui_payload(entry.get("payload") or {}, cycles=cycles)
        for row in rows:
            key = (row["dsp"].upper(), row["date"])
            if not row["routes"]:
                continue
            out[key] = max(out.get(key, 0), int(row["routes"]))
    return out


__all__ = [
    "SIPHON_DEFAULT_REGION",
    "SIPHON_HOST",
    "SIPHON_LOGIN_URL",
    "SIPHON_SPA_HOST",
    "SIPHON_SUI_URL",
    "SiphonAuthError",
    "SiphonError",
    "SiphonNetworkError",
    "SiphonRequestError",
    "amazon_week_for",
    "fetch_okami_baseline_map",
    "fetch_sui_week",
    "fetch_sui_weeks",
    "flatten_sui_payload",
    "merge_weeks_to_routes",
    "next_three_weeks",
    "siphon_login_status",
]
