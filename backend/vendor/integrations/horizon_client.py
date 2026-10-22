"""Horizon (Labor Planning) integration — process-path rates for the L5 Labor Calculator.

Horizon's SPA (``horizon.harmony.a2z.com/amzl``) reads its data from an
API-Gateway backend (``*.execute-api.us-west-2.amazonaws.com/prod/*``) that
requires **AWS SigV4** signing with *temporary* STS credentials. Those creds
are minted by ``POST https://horizon.eu-west-1.api.harmony.a2z.com/v1/sso/assume``
authenticated with the operator's Midway session cookies (read from Firefox,
same flow as the other integrations).

Auth chain (mirrors the browser, verified from a HAR capture):

1. Read ``amzn_sso_token`` / ``amzn_sso_rfp`` from Firefox scoped to
   ``horizon.eu-west-1.api.harmony.a2z.com``.
2. ``POST /v1/sso/assume`` with ``{"roleArn": HARMONY_API_GATEWAY_ROLE}`` ->
   ``{accessKeyId, secretAccessKey, sessionToken, expiration}``.
3. SigV4-sign (service ``execute-api``, region ``us-west-2``) the GET calls to
   the API-Gateway endpoints (``/prod/primaries``, ``/prod/baselines``, ...).

The ``baselines`` endpoint carries every rate + planning parameter as a row
``{metric_name, primary_id, "<YYYY-MM-DD>": value, ...}`` — one value per date
in the requested window. We select the value for the target (plan) date and
map the high-confidence ``rate_*`` / parameter metrics onto the Labor
Calculator's editable fields, returning the full raw set alongside.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
from typing import Any
from urllib.parse import quote

import requests

from app.integrations.firefox_cookies import (
    HORIZON_API_HOST,
    HORIZON_REQUIRED_COOKIES,
    has_required,
    horizon_cookies_from_profile,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ASSUME_URL = f"https://{HORIZON_API_HOST}/v1/sso/assume"
LOGIN_STATUS_URL = f"https://{HORIZON_API_HOST}/sso/login"
LOGIN_URL = "https://horizon.harmony.a2z.com/horizon/amzl"
# Role the SPA assumes to reach the API-Gateway backend (from the HAR).
HARMONY_API_GATEWAY_ROLE = "arn:aws:iam::790323939777:role/HarmonyAPIGatewayAccessRole"

# API-Gateway backend for the ``amzl`` domain.
API_HOST = "nc4yfuo6r0.execute-api.us-west-2.amazonaws.com"
API_REGION = "us-west-2"
API_SERVICE = "execute-api"
API_STAGE = "prod"
DEFAULT_DOMAIN = "amzl"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_TIMEOUT = 40.0

# How many days of the plan horizon to request (rates can vary week to week).
_WINDOW_DAYS = 90


class HorizonError(RuntimeError):
    """Horizon returned an error or unexpected payload."""


class HorizonAuthError(HorizonError):
    """The Midway/Horizon session is missing or expired."""


# ---------------------------------------------------------------------------
# Metric -> Labor Calculator field mapping
#
# Keys are the Horizon ``baselines`` metric_name; values are the exact field
# names the Labor Calculator uses (see ``LaborCalculator.vue``). Only
# high-confidence matches are mapped; every metric is still returned raw so the
# operator can verify and adjust.
# ---------------------------------------------------------------------------
NS_RATE_MAP: dict[str, str] = {
    "rate_waterspider": "Inbound Dock W/S (Waterspider)",
    "rate_diverter": "Diverter",
    "rate_picker": "Pick to Buffer",
    "rate_stower": "Container Building (Stow)",
    "rate_nonconmanualhandling": "Non-Scan Stow",
    # The Labour Planner INPUTS rows D22:D24 map these three distinct ASML
    # baselines to the three manual-induct positions. Do not reuse them for
    # Feeder ASL / Auto Scan Pusher, whose planner inputs are separate.
    "rate_asml_loader": "Induct Line Loader (Feeder) manual",
    "rate_asml_labeler": "Induct manual",
    "rate_asml_pusher": "Pusher manual",
    "rate_ad_stower": "Auto Divert Stower",
    "rate_adta_diverter_straightener": "Auto Divert Straightener",
    "rate_problem_solver": "PS/Quality/Process Guide",
}
NS_PARAM_MAP: dict[str, str] = {
    "sortation_length_nd_cycle1": "sortation_length_1",
    "sortation_length_nd_cycle2": "sortation_length_2",
    "fpp_asl": "flow_path_pct_asl",
    "fpp_adta": "flow_path_pct_adta",
    "power_hour_share": "power_hour_share",
}
AMPT_RATE_MAP: dict[str, str] = {
    "rate_pick_stage": "Pick & Stage",
    "rate_stow_bag_replenishment": "Stow bag replenishment",
}
AMPT_PARAM_MAP: dict[str, str] = {
    "staging_length_extra_nd_cycle1": "staging_length",
    "sortation_length_ampt": "ps_length",
}


# ---------------------------------------------------------------------------
# Cookies / session
# ---------------------------------------------------------------------------
def _read_cookies() -> dict[str, str]:
    try:
        return horizon_cookies_from_profile()
    except Exception:  # noqa: BLE001 - treated as "no session"
        return {}


def login_status() -> dict[str, Any]:
    """Best-effort check that the operator has a live Horizon Midway session."""
    cookies = _read_cookies()
    if not has_required(cookies, HORIZON_REQUIRED_COOKIES):
        return {
            "ready": False,
            "reason": "no_cookies",
            "message": "No hay sesion de Horizon. Inicia sesion en Firefox.",
            "login_url": LOGIN_URL,
        }
    try:
        resp = requests.get(
            LOGIN_STATUS_URL,
            headers={"User-Agent": _UA, "Accept": "*/*"},
            cookies=cookies,
            timeout=15.0,
            allow_redirects=False,
        )
        info = resp.json() if resp.status_code == 200 else {}
        ready = bool(info.get("is_authenticated"))
    except (requests.RequestException, ValueError):
        # Cookies exist; trust them and let the actual fetch surface auth errors.
        ready = True
    return {
        "ready": ready,
        "reason": "" if ready else "expired",
        "message": "" if ready else "Sesion de Horizon expirada. Reinicia sesion en Firefox.",
        "login_url": LOGIN_URL,
    }


def _assume_credentials(cookies: dict[str, str]) -> dict[str, str]:
    """Exchange the Midway session cookies for temporary AWS credentials."""
    try:
        resp = requests.post(
            ASSUME_URL,
            data=json.dumps({"roleArn": HARMONY_API_GATEWAY_ROLE}),
            headers={
                "User-Agent": _UA,
                "Accept": "*/*",
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": "https://horizon.harmony.a2z.com",
                "Referer": LOGIN_URL,
            },
            cookies=cookies,
            timeout=_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise HorizonError(f"No se pudo contactar Horizon: {exc}") from exc

    if resp.status_code in (401, 403):
        raise HorizonAuthError(
            "Sesion de Horizon expirada. Abre https://horizon.harmony.a2z.com/horizon/amzl en Firefox."
        )
    if resp.status_code != 200:
        raise HorizonError(f"Horizon /sso/assume devolvio HTTP {resp.status_code}.")

    try:
        data = resp.json()
    except ValueError as exc:
        raise HorizonError("Horizon /sso/assume devolvio una respuesta no-JSON.") from exc

    access = data.get("accessKeyId")
    secret = data.get("secretAccessKey")
    token = data.get("sessionToken")
    if not (access and secret and token):
        # A well-formed response without creds usually means the session is dead.
        raise HorizonAuthError(
            "Horizon no devolvio credenciales. Reinicia sesion en Firefox."
        )
    return {
        "access_key": access,
        "secret_key": secret,
        "session_token": token,
        "expiration": data.get("expiration", ""),
    }


# ---------------------------------------------------------------------------
# Minimal AWS SigV4 (GET, empty body) for execute-api / us-west-2
# ---------------------------------------------------------------------------
def _canonical_query(params: dict[str, str]) -> str:
    parts = []
    for key in sorted(params):
        k = quote(str(key), safe="")
        v = quote(str(params[key]), safe="")
        parts.append(f"{k}={v}")
    return "&".join(parts)


def _sign_key(secret: str, datestamp: str) -> bytes:
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, API_REGION)
    k_service = _hmac(k_region, API_SERVICE)
    return _hmac(k_service, "aws4_request")


def _signed_get(creds: dict[str, str], path: str, params: dict[str, str]) -> requests.Response:
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    canonical_query = _canonical_query(params)
    signed_headers = "host;x-amz-date;x-amz-security-token"
    canonical_headers = (
        f"host:{API_HOST}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-security-token:{creds['session_token']}\n"
    )
    canonical_request = "\n".join(
        ["GET", path, canonical_query, canonical_headers, signed_headers, _EMPTY_SHA256]
    )
    scope = f"{datestamp}/{API_REGION}/{API_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _sign_key(creds["secret_key"], datestamp),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={creds['access_key']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    url = f"https://{API_HOST}{path}"
    if canonical_query:
        url = f"{url}?{canonical_query}"
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Host": API_HOST,
        "x-amz-date": amz_date,
        "x-amz-security-token": creds["session_token"],
        "Authorization": authorization,
        "Origin": "https://horizon.harmony.a2z.com",
        "Referer": "https://horizon.harmony.a2z.com/",
    }
    try:
        return requests.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise HorizonError(f"Fallo la peticion a Horizon ({path}): {exc}") from exc


def _fetch_rows(creds: dict[str, str], endpoint: str, params: dict[str, str]) -> list[dict[str, Any]]:
    """GET an API-Gateway endpoint and unwrap the ``{"data": "<json string>"}`` envelope."""
    resp = _signed_get(creds, f"/{API_STAGE}/{endpoint}", params)
    if resp.status_code in (401, 403):
        raise HorizonAuthError("Credenciales de Horizon rechazadas. Reinicia sesion.")
    if resp.status_code != 200:
        raise HorizonError(f"Horizon /{endpoint} devolvio HTTP {resp.status_code}.")
    try:
        outer = resp.json()
        data = outer.get("data") if isinstance(outer, dict) else outer
        if isinstance(data, str):
            data = json.loads(data)
    except ValueError as exc:
        raise HorizonError(f"Horizon /{endpoint} devolvio JSON invalido.") from exc
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Business helpers
# ---------------------------------------------------------------------------
def _resolve_primary_id(creds: dict[str, str], station: str) -> str:
    """Map a bare station code (``DCZ4``) to its Horizon primary id (``ES-DCZ4``)."""
    station = (station or "").strip().upper()
    if not station:
        raise HorizonError("Falta la estacion.")
    if "-" in station:
        return station  # already a primary id
    rows = _fetch_rows(creds, "primaries", {"domain": DEFAULT_DOMAIN, "stage": API_STAGE})
    for row in rows:
        if str(row.get("station", "")).strip().upper() == station:
            pid = str(row.get("primary_id", "")).strip()
            if pid:
                return pid
            country = str(row.get("country", "")).strip().upper()
            if country:
                return f"{country}-{station}"
    raise HorizonError(f"Estacion '{station}' no encontrada en Horizon.")


def _date_columns(row: dict[str, Any]) -> list[str]:
    return sorted(
        k for k in row
        if isinstance(k, str) and len(k) == 10 and k[4] == "-" and k[:4].isdigit()
    )


def _select_value(row: dict[str, Any], target_date: str | None) -> float | None:
    """Pick the metric value for ``target_date`` (else the nearest future / first)."""
    cols = _date_columns(row)
    if not cols:
        return None
    chosen = None
    if target_date and target_date in cols:
        chosen = target_date
    elif target_date:
        future = [c for c in cols if c >= target_date]
        chosen = future[0] if future else cols[-1]
    else:
        chosen = cols[0]
    try:
        val = row.get(chosen)
        return float(val) if val is not None and val != "" else None
    except (TypeError, ValueError):
        return None


def _default_window(target_date: str | None) -> tuple[str, str]:
    today = _dt.date.today()
    if target_date:
        try:
            start = _dt.date.fromisoformat(target_date)
        except ValueError:
            start = today
    else:
        start = today
    end = start + _dt.timedelta(days=_WINDOW_DAYS)
    return start.isoformat(), end.isoformat()


def fetch_rates(*, station: str, target_date: str | None = None) -> dict[str, Any]:
    """Return the Horizon process-path rates for ``station`` on ``target_date``.

    Result shape::

        {
          "station": "DCZ4", "primary_id": "ES-DCZ4", "date": "2026-07-12",
          "ns_rates":    {"<calc rate name>": value, ...},
          "ns_params":   {"<calc param name>": value, ...},
          "ampt_rates":  {"<calc rate name>": value, ...},
          "ampt_params": {"<calc param name>": value, ...},
          "raw":         {"<horizon metric_name>": value, ...},
          "unmapped":    ["<horizon metric_name>", ...],
        }
    """
    cookies = _read_cookies()
    if not has_required(cookies, HORIZON_REQUIRED_COOKIES):
        raise HorizonAuthError(
            "No hay sesion de Horizon. Inicia sesion en https://horizon.harmony.a2z.com/horizon/amzl."
        )

    creds = _assume_credentials(cookies)
    primary_id = _resolve_primary_id(creds, station)
    start, end = _default_window(target_date)
    rows = _fetch_rows(
        creds,
        "baselines",
        {
            "domain": DEFAULT_DOMAIN,
            "primary_ids": primary_id,
            "start_date": start,
            "end_date": end,
            "stage": API_STAGE,
        },
    )
    if not rows:
        raise HorizonError(f"Horizon no devolvio baselines para {primary_id}.")

    raw: dict[str, float] = {}
    for row in rows:
        name = str(row.get("metric_name", "")).strip()
        if not name:
            continue
        value = _select_value(row, target_date)
        if value is not None:
            raw[name] = value

    def _mapped(mapping: dict[str, str]) -> dict[str, float]:
        return {label: raw[metric] for metric, label in mapping.items() if metric in raw}

    def _sources(mapping: dict[str, str]) -> dict[str, str]:
        # {calc field label -> horizon metric_name} for the metrics present.
        return {label: metric for metric, label in mapping.items() if metric in raw}

    mapped_keys = set(NS_RATE_MAP) | set(NS_PARAM_MAP) | set(AMPT_RATE_MAP) | set(AMPT_PARAM_MAP)
    return {
        "station": (station or "").strip().upper(),
        "primary_id": primary_id,
        "date": target_date or start,
        "ns_rates": _mapped(NS_RATE_MAP),
        "ns_params": _mapped(NS_PARAM_MAP),
        "ampt_rates": _mapped(AMPT_RATE_MAP),
        "ampt_params": _mapped(AMPT_PARAM_MAP),
        "suggested_sources": {
            **_sources(NS_RATE_MAP),
            **_sources(NS_PARAM_MAP),
            **_sources(AMPT_RATE_MAP),
            **_sources(AMPT_PARAM_MAP),
        },
        "raw": raw,
        "unmapped": sorted(k for k in raw if k not in mapped_keys),
    }


def fetch_rates_window(
    *,
    station: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Return Horizon baselines rates for **every day** in a date window.

    Unlike :func:`fetch_rates` (which collapses the window to a single plan
    date), this returns one metric map per calendar day so the daily metrics
    store can persist the rates of each day. Result shape::

        {
          "station": "DCZ4", "primary_id": "ES-DCZ4",
          "start": "2026-07-19", "end": "2026-08-02",
          "days": {
             "2026-07-19": {
                "raw": {"<metric_name>": value, ...},
                "ns_rates": {...}, "ns_params": {...},
                "ampt_rates": {...}, "ampt_params": {...},
             },
             ...
          },
          "metrics": ["<metric_name>", ...],  # union across days, sorted
        }
    """
    cookies = _read_cookies()
    if not has_required(cookies, HORIZON_REQUIRED_COOKIES):
        raise HorizonAuthError(
            "No hay sesion de Horizon. Inicia sesion en https://horizon.harmony.a2z.com/horizon/amzl."
        )

    creds = _assume_credentials(cookies)
    primary_id = _resolve_primary_id(creds, station)

    today = _dt.date.today()
    if start_date:
        start = start_date.strip()
    else:
        # Mirror Horizon's SPA request exactly: the planning horizon begins on
        # the Sunday that starts the *next* week and spans 90 days. Keep the
        # W-1 snapshot strict on Sundays too: once a Horizon week starts, its
        # rates come from the workbook downloaded during the prior week and
        # are no longer overwritten as W0.
        days_to_sunday = (6 - today.weekday()) % 7 or 7
        start = (today + _dt.timedelta(days=days_to_sunday)).isoformat()
    if end_date:
        end = end_date.strip()
    else:
        try:
            end = (_dt.date.fromisoformat(start) + _dt.timedelta(days=90)).isoformat()
        except ValueError:
            end = (today + _dt.timedelta(days=90)).isoformat()

    rows = _fetch_rows(
        creds,
        "baselines",
        {
            "domain": DEFAULT_DOMAIN,
            "primary_ids": primary_id,
            "start_date": start,
            "end_date": end,
            "stage": API_STAGE,
        },
    )
    if not rows:
        raise HorizonError(f"Horizon no devolvio baselines para {primary_id}.")

    # Every date column present across the rows, restricted to the window.
    dates: set[str] = set()
    for row in rows:
        for col in _date_columns(row):
            if start <= col <= end:
                dates.add(col)

    mapped_keys = set(NS_RATE_MAP) | set(NS_PARAM_MAP) | set(AMPT_RATE_MAP) | set(AMPT_PARAM_MAP)
    days: dict[str, Any] = {}
    for date in sorted(dates):
        raw: dict[str, float] = {}
        for row in rows:
            name = str(row.get("metric_name", "")).strip()
            if not name:
                continue
            value = row.get(date)
            try:
                fval = float(value) if value is not None and value != "" else None
            except (TypeError, ValueError):
                fval = None
            if fval is not None:
                raw[name] = fval
        if not raw:
            continue

        def _mapped(mapping: dict[str, str], _raw: dict[str, float] = raw) -> dict[str, float]:
            return {label: _raw[metric] for metric, label in mapping.items() if metric in _raw}

        days[date] = {
            "raw": raw,
            "ns_rates": _mapped(NS_RATE_MAP),
            "ns_params": _mapped(NS_PARAM_MAP),
            "ampt_rates": _mapped(AMPT_RATE_MAP),
            "ampt_params": _mapped(AMPT_PARAM_MAP),
            "unmapped": sorted(k for k in raw if k not in mapped_keys),
        }

    metrics = sorted({k for day in days.values() for k in day["raw"]})
    return {
        "station": (station or "").strip().upper(),
        "primary_id": primary_id,
        "start": start,
        "end": end,
        "days": days,
        "metrics": metrics,
    }


def fetch_plan_rates_window(
    *,
    station: str,
    start_date: str,
    end_date: str,
    metric_maps: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Download Forecast UTR's daily rates and parameters from Horizon.

    The original Labor Planner looks up each date in its ``INPUTS`` sheet,
    then calculates HC per position from the day's rate, duration and flow
    path percentages. Preserve every ``rate_*`` / ``auto_rate_*`` metric and
    also return the known calculator parameters in mapped groups so the UI
    can reproduce that calculation without guessing internal metric names.

    ``metric_maps`` (optional) replaces the default Horizon-metric -> label
    maps per group (``ns_rates`` / ``ns_params`` / ``ampt_rates`` /
    ``ampt_params``): Horizon metric names are NOT the same on every site,
    so stations can remap them from Configuration (Forecast UTR sheet).
    Metrics referenced by an override are kept even when they don't match
    the ``rate_*`` prefixes.
    """
    station = (station or "").strip().upper()
    try:
        start = _dt.date.fromisoformat((start_date or "").strip())
        end = _dt.date.fromisoformat((end_date or "").strip())
    except ValueError as exc:
        raise HorizonError("Las fechas del plan de Horizon no son validas.") from exc
    if end < start:
        raise HorizonError("El rango del plan de Horizon no es valido.")

    cookies = _read_cookies()
    if not has_required(cookies, HORIZON_REQUIRED_COOKIES):
        raise HorizonAuthError(
            f"No hay sesion de Horizon. Inicia sesion en {LOGIN_URL}."
        )

    creds = _assume_credentials(cookies)
    primary_id = _resolve_primary_id(creds, station)
    rows = _fetch_rows(
        creds,
        "inputs",
        {
            "domain": DEFAULT_DOMAIN,
            "primary_ids": primary_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "stage": API_STAGE,
        },
    )
    if not rows:
        raise HorizonError(f"Horizon no devolvio el plan para {primary_id}.")

    date_keys = [
        (start + _dt.timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]
    maps: dict[str, dict[str, str]] = {
        "ns_rates": NS_RATE_MAP,
        "ns_params": NS_PARAM_MAP,
        "ampt_rates": AMPT_RATE_MAP,
        "ampt_params": AMPT_PARAM_MAP,
    }
    if metric_maps:
        # Los grupos del caller REEMPLAZAN los defaults y pueden añadir
        # grupos nuevos (p. ej. pm_rates/pm_params del Forecast UTR).
        for group, mapping in metric_maps.items():
            if isinstance(mapping, dict):
                maps[group] = mapping
    mapped_keys = set().union(*(set(mapping) for mapping in maps.values()))
    raw_days: dict[str, dict[str, float]] = {date_key: {} for date_key in date_keys}
    # Además de los rate_*/auto_rate_* y de las métricas ya mapeadas, se
    # conservan las que PARECEN parámetros del cálculo (length/fpp/share/
    # pct/threshold): los sites renombran métricas y el automapeo del
    # Forecast UTR necesita verlas en ``raw``/``metrics`` para casarlas.
    _paramish = ("length", "fpp", "flow_path", "share", "pct", "threshold")
    for row in rows:
        metric = str(row.get("metric_name") or "").strip()
        if not metric:
            continue
        metric_lower = metric.lower()
        if not (
            metric.startswith("rate_")
            or metric.startswith("auto_rate_")
            or metric in mapped_keys
            or any(token in metric_lower for token in _paramish)
        ):
            continue
        for date_key in date_keys:
            value = row.get(date_key)
            try:
                numeric = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                numeric = None
            if numeric is not None:
                raw_days[date_key][metric] = numeric

    def _mapped(raw: dict[str, float], mapping: dict[str, str]) -> dict[str, float]:
        return {label: raw[metric] for metric, label in mapping.items() if metric in raw}

    days: dict[str, dict[str, Any]] = {}
    for date_key, raw in raw_days.items():
        if not raw:
            continue
        days[date_key] = {
            "raw": raw,
            **{group: _mapped(raw, mapping) for group, mapping in maps.items()},
        }

    metrics = sorted({metric for values in raw_days.values() for metric in values})
    if not metrics:
        raise HorizonError(f"Horizon no devolvio rates del plan para {primary_id}.")
    return {
        "station": station,
        "primary_id": primary_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source": "horizon_inputs",
        "days": days,
        "metrics": metrics,
    }


__all__ = [
    "HorizonAuthError",
    "HorizonError",
    "LOGIN_URL",
    "fetch_plan_rates_window",
    "fetch_rates",
    "fetch_rates_window",
    "login_status",
]
