"""Metis (metis.step.amazon.dev) integration helpers.

The DO "Forecast +3Wks (Metis)" tool consumes two CSV exports that the
Metis SPA lets the operator download from *View & Override Plans*:

* **Station Plan** — ``GET /fetch_station_level_csv``
* **DSP Plan**     — ``GET /fetch_dsp_level_csv``

Both are scoped by ``planType`` / ``planId`` / ``amazonWeek`` / ``country``.
To resolve the "most recent plan" the SPA first calls
``POST /fetch_plan_ids`` which returns the available *baselines* (newest
first) and *scenarios* (one of which is flagged ``isActiveScenario``).

Like Siphon / Mercury / PerfectMile we skip Selenium: once the operator
has logged into Midway, the relevant cookies (``amzn_sso_token`` /
``amzn_sso_rfp``) live in their Firefox profile and we forward them.

Public API:

* :func:`metis_login_status` — checks the Firefox cookies.
* :func:`fetch_plan_ids` — lists baselines + scenarios for a week/country.
* :func:`resolve_active_plan` — picks the active scenario (or newest
  baseline) for a week/country.
* :func:`download_station_csv` / :func:`download_dsp_csv` — raw CSV bytes.
* :func:`fetch_active_plans` — high-level: resolve + download both CSVs.

Errors mirror the Siphon client:

* :class:`MetisAuthError` — cookies missing / token expired / Midway redirect.
* :class:`MetisNetworkError` — DNS / TLS / connect issues.
* :class:`MetisRequestError` — Metis returned a non-2xx / non-CSV response.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any

import requests

from app.integrations.firefox_cookies import (
    METIS_HOST_PATTERNS,
    METIS_REQUIRED_COOKIES,
    has_required,
    read_cookies_from_profile,
)


METIS_SITE = "https://metis.step.amazon.dev"
METIS_API_HOST = "api.metis.step.amazon.dev"
METIS_API_BASE = f"https://{METIS_API_HOST}"
METIS_LOGIN_URL = f"{METIS_SITE}/"
METIS_DEFAULT_TIMEOUT = 40.0
METIS_DEFAULT_COUNTRY = "ES"

# 2026-08: Metis fusionó el desplegable de país de España en "ES&PT".
# Los planes nuevos (WK35+) cuelgan de ese código; los antiguos siguen
# bajo "ES". `resolve_active_plan` prueba los candidatos en orden.
METIS_MERGED_IBERIA = "ES&PT"


def _country_candidates(country: str | None) -> list[str]:
    """Expand ``country`` into the Metis dropdown codes to try, in order.

    "ES" / "PT" → merged "ES&PT" first, legacy code second (plans filed
    before the merge). "ES&PT" → itself first, then "ES" (in case Metis
    reverts the merge). Anything else passes through untouched.
    """
    code = (country or METIS_DEFAULT_COUNTRY).strip().upper()
    if code in {"ES", "PT"}:
        return [METIS_MERGED_IBERIA, code]
    if code == METIS_MERGED_IBERIA:
        return [METIS_MERGED_IBERIA, "ES"]
    return [code]

# api.metis.step.amazon.dev cookie scoping target.
_COOKIE_TARGET_HOST = METIS_API_HOST


_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,es-ES;q=0.8,es;q=0.5,en;q=0.3",
    "Connection": "keep-alive",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    ),
    "Referer": f"{METIS_SITE}/",
    "Origin": METIS_SITE,
}

# Only forward the auth-relevant cookies (drop tracking / unrelated).
_COOKIE_WHITELIST: tuple[str, ...] = (
    "amzn_sso_token",
    "amzn_sso_rfp",
    "user",
    "realm",
    "domain",
    "enable_midway",
    "session-id",
    "session-id-time",
    "session-token",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MetisError(RuntimeError):
    """Base class for Metis errors."""


class MetisAuthError(MetisError):
    """Raised when the user has no usable Metis session."""


class MetisNetworkError(MetisError):
    """Connectivity / TLS / timeout issues."""


class MetisRequestError(MetisError):
    """Metis answered with a non-2xx status or an unexpected payload."""


# ---------------------------------------------------------------------------
# Cookies / auth
# ---------------------------------------------------------------------------


def _read_cookies() -> dict[str, str]:
    try:
        return read_cookies_from_profile(
            METIS_HOST_PATTERNS,
            target_host=_COOKIE_TARGET_HOST,
        )
    except Exception:
        return {}


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


def metis_login_status() -> dict[str, Any]:
    """Mirror :func:`siphon_login_status` for the Metis tool."""
    cookies = _read_cookies()
    if not cookies:
        return {
            "ready": False,
            "missing": list(METIS_REQUIRED_COOKIES),
            "cookie_count": 0,
            "token_expired": False,
            "login_url": METIS_LOGIN_URL,
        }
    missing = [name for name in METIS_REQUIRED_COOKIES if name not in cookies]
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
        "login_url": METIS_LOGIN_URL,
        "user": cookies.get("user", ""),
    }


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------


def current_amazon_week(date: _dt.date | None = None) -> str:
    """Return the current Amazon week as ``"YYYY-WW"`` (ISO week numbers).

    Metis labels weeks ``2026-25`` etc.; ISO weeks track the Amazon week
    closely enough for resolving the "most recent" plan.
    """
    target = date or _dt.date.today()
    iso_year, iso_week, _ = target.isocalendar()
    return f"{iso_year}-{iso_week:02d}"


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------


def _resolve_verify(verify: bool | str | None) -> bool | str:
    if verify is not None:
        return verify
    env_verify = os.environ.get("METIS_VERIFY_TLS", "").strip().lower()
    if env_verify in {"0", "false", "no", "off"}:
        return False
    return True


def _session_cookies(cookies: dict[str, str] | None) -> dict[str, str]:
    session_cookies = cookies if cookies is not None else _read_cookies()
    if not has_required(session_cookies, METIS_REQUIRED_COOKIES):
        raise MetisAuthError(
            "No hay cookies de Metis. Inicia sesion en metis.step.amazon.dev en Firefox."
        )
    session_cookies = _filter_cookies(session_cookies)
    if not has_required(session_cookies, METIS_REQUIRED_COOKIES):
        raise MetisAuthError(
            "Cookies Metis filtradas pero faltan las requeridas. Vuelve a iniciar sesion."
        )
    return session_cookies


def _check_auth_response(response: requests.Response) -> None:
    if response.status_code in (301, 302, 303, 307, 308):
        raise MetisAuthError(
            "Metis redirigio a Midway. La sesion expiro — vuelve a abrir metis.step.amazon.dev."
        )
    if response.status_code in (401, 403):
        raise MetisAuthError(
            f"Metis respondio {response.status_code}. Vuelve a iniciar sesion."
        )


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = METIS_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> requests.Response:
    url = f"{METIS_API_BASE}{path}"
    session_cookies = _session_cookies(cookies)
    headers = dict(_DEFAULT_HEADERS)
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            cookies=session_cookies,
            timeout=timeout,
            allow_redirects=False,
            verify=_resolve_verify(verify),
        )
    except requests.exceptions.Timeout as exc:
        raise MetisNetworkError(
            f"Metis no respondio en {timeout:.0f}s. Revisa la VPN."
        ) from exc
    except requests.exceptions.SSLError as exc:
        raise MetisNetworkError(
            "Error SSL con Metis (proxy corporativo). "
            "Confirma el root cert o desactiva la verificacion con METIS_VERIFY_TLS=0. "
            f"Detalle: {exc}"
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise MetisNetworkError(
            f"No se pudo conectar a {METIS_API_HOST}. Revisa la VPN. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise MetisNetworkError(f"Error de red contra {METIS_API_HOST}: {exc}") from exc

    _check_auth_response(response)
    return response


# ---------------------------------------------------------------------------
# Plan discovery
# ---------------------------------------------------------------------------


def fetch_plan_ids(
    *,
    week: str,
    country: str = METIS_DEFAULT_COUNTRY,
    plan_types: list[str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = METIS_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> dict[str, Any]:
    """``POST /fetch_plan_ids`` — baselines + scenarios for a week/country."""
    body: dict[str, Any] = {"amazonWeek": week, "country": country}
    if plan_types:
        body["planTypes"] = plan_types
    response = _request(
        "POST",
        "/fetch_plan_ids",
        json_body=body,
        cookies=cookies,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code != 200:
        raise MetisRequestError(
            f"fetch_plan_ids HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MetisRequestError(
            f"Respuesta no-JSON de fetch_plan_ids: {response.text[:200]}"
        ) from exc


def resolve_active_plan(
    *,
    week: str,
    country: str = METIS_DEFAULT_COUNTRY,
    cookies: dict[str, str] | None = None,
    timeout: float = METIS_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> dict[str, str]:
    """Pick the plan to download for ``week`` / ``country``.

    Preference order, mirroring the SPA's "Active Scenario" badge:

    1. The scenario flagged ``isActiveScenario: true`` → ``scenario``.
    2. Otherwise the newest scenario (by creation timestamp) → ``scenario``.
    3. Otherwise the newest baseline (list is newest-first) → ``baseline``.

    The ``country`` is expanded via :func:`_country_candidates` (e.g.
    "ES" → try the merged "ES&PT" dropdown first, then legacy "ES"); the
    returned dict carries the candidate that actually had plans, so the
    CSV downloads reuse the right code.

    Returns ``{"planType", "planId", "week", "country"}``.
    """
    last_exc: MetisRequestError | None = None
    for candidate in _country_candidates(country):
        try:
            data = fetch_plan_ids(
                week=week,
                country=candidate,
                cookies=cookies,
                timeout=timeout,
                verify=verify,
            )
        except MetisRequestError as exc:
            # A candidate the API no longer knows may 4xx/5xx — try the
            # next one. Auth / network errors propagate immediately.
            last_exc = exc
            continue
        scenarios = data.get("scenarioNames") or []
        if isinstance(scenarios, list) and scenarios:
            active = next(
                (
                    s
                    for s in scenarios
                    if isinstance(s, dict) and s.get("isActiveScenario")
                ),
                None,
            )
            chosen = active
            if chosen is None:
                # Newest scenario by creation timestamp.
                def _ts(s: dict[str, Any]) -> str:
                    return str(s.get("scenarioCreationTimestamp") or "")

                dict_scenarios = [s for s in scenarios if isinstance(s, dict)]
                if dict_scenarios:
                    chosen = max(dict_scenarios, key=_ts)
            if chosen and chosen.get("scenarioName"):
                return {
                    "planType": "scenario",
                    "planId": str(chosen["scenarioName"]),
                    "week": week,
                    "country": candidate,
                }

        baselines = data.get("baselineNames") or []
        if isinstance(baselines, list) and baselines:
            # The list is returned newest-first.
            return {
                "planType": "baseline",
                "planId": str(baselines[0]),
                "week": week,
                "country": candidate,
            }

    raise last_exc or MetisRequestError(
        f"Metis no devolvio planes para {country} / {week}."
    )


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def _download_csv(
    path: str,
    *,
    plan_type: str,
    plan_id: str,
    week: str,
    country: str,
    cookies: dict[str, str] | None,
    timeout: float,
    verify: bool | str | None,
) -> bytes:
    params = {
        "planType": plan_type,
        "planId": plan_id,
        "amazonWeek": week,
        "country": country,
    }
    response = _request(
        "GET",
        path,
        params=params,
        cookies=cookies,
        timeout=timeout,
        verify=verify,
    )
    if response.status_code != 200:
        raise MetisRequestError(
            f"{path} HTTP {response.status_code}: {response.text[:200]}"
        )
    content = response.content or b""
    if not content:
        raise MetisRequestError(f"{path} devolvio un CSV vacio.")
    # Guard against an HTML login page sneaking through with a 200.
    head = content[:64].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise MetisAuthError(
            "Metis devolvio HTML en lugar de CSV — sesion expirada, vuelve a iniciar sesion."
        )
    return content


def download_station_csv(
    *,
    plan_type: str,
    plan_id: str,
    week: str,
    country: str = METIS_DEFAULT_COUNTRY,
    cookies: dict[str, str] | None = None,
    timeout: float = METIS_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> bytes:
    """``GET /fetch_station_level_csv`` — the Station Plan CSV bytes."""
    return _download_csv(
        "/fetch_station_level_csv",
        plan_type=plan_type,
        plan_id=plan_id,
        week=week,
        country=country,
        cookies=cookies,
        timeout=timeout,
        verify=verify,
    )


def download_dsp_csv(
    *,
    plan_type: str,
    plan_id: str,
    week: str,
    country: str = METIS_DEFAULT_COUNTRY,
    cookies: dict[str, str] | None = None,
    timeout: float = METIS_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> bytes:
    """``GET /fetch_dsp_level_csv`` — the DSP Plan CSV bytes."""
    return _download_csv(
        "/fetch_dsp_level_csv",
        plan_type=plan_type,
        plan_id=plan_id,
        week=week,
        country=country,
        cookies=cookies,
        timeout=timeout,
        verify=verify,
    )


def fetch_active_plans(
    *,
    week: str | None = None,
    country: str = METIS_DEFAULT_COUNTRY,
    cookies: dict[str, str] | None = None,
    timeout: float = METIS_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> dict[str, Any]:
    """Resolve the most recent plan and download both CSVs.

    Returns::

        {
            "plan": {"planType", "planId", "week", "country"},
            "dsp_csv": bytes,
            "station_csv": bytes,
            "dsp_filename": "...",
            "station_filename": "...",
        }
    """
    # Read cookies once and reuse across the 3 calls.
    session_cookies = cookies if cookies is not None else _read_cookies()
    if week:
        # Explicit week: strict — the caller asked for that exact plan.
        plan = resolve_active_plan(
            week=week,
            country=country,
            cookies=session_cookies,
            timeout=timeout,
            verify=verify,
        )
    else:
        # No week given: prefer the current week but fall back to previous
        # ones. Metis publishes each 3-week plan DURING the prior week, so
        # early in the week the freshest available plan usually still hangs
        # off the previous Amazon week (e.g. on Tue of WK33 the newest plan
        # may be filed under WK32). Auth / network errors propagate
        # immediately — only "no plans for this week" walks back.
        plan = None
        last_exc: MetisRequestError | None = None
        for back in range(0, 5):
            day = _dt.date.today() - _dt.timedelta(weeks=back)
            candidate_week = current_amazon_week(day)
            try:
                plan = resolve_active_plan(
                    week=candidate_week,
                    country=country,
                    cookies=session_cookies,
                    timeout=timeout,
                    verify=verify,
                )
                break
            except MetisRequestError as exc:
                last_exc = exc
        if plan is None:
            raise last_exc or MetisRequestError(
                f"Metis no devolvio planes para {country}."
            )
    dsp_csv = download_dsp_csv(
        plan_type=plan["planType"],
        plan_id=plan["planId"],
        week=plan["week"],
        country=plan["country"],
        cookies=session_cookies,
        timeout=timeout,
        verify=verify,
    )
    station_csv = download_station_csv(
        plan_type=plan["planType"],
        plan_id=plan["planId"],
        week=plan["week"],
        country=plan["country"],
        cookies=session_cookies,
        timeout=timeout,
        verify=verify,
    )
    safe_plan = "".join(c for c in plan["planId"] if c.isalnum() or c in "-_") or "plan"
    return {
        "plan": plan,
        "dsp_csv": dsp_csv,
        "station_csv": station_csv,
        "dsp_filename": f"metis_dsp_{safe_plan}_{plan['week']}_{plan['country']}.csv",
        "station_filename": f"metis_station_{safe_plan}_{plan['week']}_{plan['country']}.csv",
    }


__all__ = [
    "METIS_API_BASE",
    "METIS_API_HOST",
    "METIS_DEFAULT_COUNTRY",
    "METIS_LOGIN_URL",
    "METIS_MERGED_IBERIA",
    "METIS_SITE",
    "MetisAuthError",
    "MetisError",
    "MetisNetworkError",
    "MetisRequestError",
    "current_amazon_week",
    "download_dsp_csv",
    "download_station_csv",
    "fetch_active_plans",
    "fetch_plan_ids",
    "metis_login_status",
    "resolve_active_plan",
]
