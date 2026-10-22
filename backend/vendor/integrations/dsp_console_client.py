"""DSP console (``logistics.amazon.es/internal/dspconsole``) fleet client.

Fetches the per-DSP TOTAL fleet ("registered vehicles") that the DSP
console BHD page shows, straight from its data API:

    GET https://logistics.amazon.es/internal/performance/api/v1/getVehicleSummaries
        ?dspId=<DSP_ID_<CODE>>&serviceAreaId=<CORTEX_SERVICE_AREA_ID>
        &from=<today>&to=<today+3>

Validated against the user's HAR (``logistics.amazon.es_Archive
[26-08-19 12-42-10].har``, DCZ4):

* Plain cookie auth — the same Firefox ``logistics-federate-token``
  session the Cortex integration reads. NO ``X-Cortex-*`` HMAC signing
  on this endpoint.
* ``dspId`` == the DSP console ``companyId`` == the configured
  ``DSP_ID_<CODE>`` (verified: DSP_ID_DESL / DSP_ID_CDOR match the HAR).
* ``serviceAreaId`` == the configured ``CORTEX_SERVICE_AREA_ID`` for the
  station (verified: DCZ4 -> 7afaeae3-…).
* Response shape: ``tableData["dsp-station-vehicle-summaries"].rows`` is
  a list of JSON *strings*, one per (dsp, area, window):
  ``{"registered_vehicles": 40, "active_vehicles": 38,
     "grounded_vehicles": 2, "out_of_service_vehicles": 0, ...}``.
  ``registered_vehicles`` is the "total fleet" figure of the console.

First consumer: Forecast +3Wks "Fleet Real" (the Flota Total per DSP is
pre-seeded from here; the operator can still adjust it by hand).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import requests

from app.integrations.firefox_cookies import (
    LOGISTICS_HOST_PATTERNS,
    LOGISTICS_REQUIRED_COOKIES,
    has_required,
    logistics_cookies_from_profile,
    session_status,
)

DSP_CONSOLE_HOST = "logistics.amazon.es"
DSP_CONSOLE_BASE_URL = f"https://{DSP_CONSOLE_HOST}"
_VEHICLE_SUMMARIES_PATH = "/internal/performance/api/v1/getVehicleSummaries"
_DEFAULT_TIMEOUT = 25.0
# Capacidad REAL para el Fleet Real (instrucción 2026-08-19, con captura
# del console): NO es el "registrado" (registered_vehicles) — son las vans
# ACTIVAS menos 2 que se reservan como backup. CDOR ejemplo: 48 registradas
# / 40 activas → capacidad real 38.
BACKUP_VANS = 2
# The console requests a 4-day window (today -> today+3); the summary row
# covers the whole window, so we mirror it verbatim.
_WINDOW_DAYS = 3

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)


class DspConsoleAuthError(RuntimeError):
    """Login required (missing/expired ``logistics-federate-token``)."""


class DspConsoleError(RuntimeError):
    """Any other DSP console fetch failure."""


def build_console_url(station: str = "", dsp_id: str = "", to_date: str = "") -> str:
    """DSP console BHD page URL (Fleet portal link / login page)."""
    params: dict[str, str] = {}
    code = (station or "").strip().upper()
    if code:
        params["station"] = code
    if (dsp_id or "").strip():
        params["companyId"] = dsp_id.strip()
    params["pageId"] = "dsp_bhd"
    params["tabId"] = "bhd-dsp-tab"
    params["timeFrame"] = "Daily"
    params["to"] = (to_date or "").strip() or date.today().isoformat()
    return f"{DSP_CONSOLE_BASE_URL}/internal/dspconsole?{urlencode(params)}"


def configured_dsp_ids() -> dict[str, str]:
    """``{dsp_code: dsp_console_companyId}`` from the station config.

    Iterates ``ALLOWED_DSPS`` and reads each ``DSP_ID_<CODE>``; DSPs
    without a configured UUID are skipped (nothing to query).
    """
    import os

    from app.core.env_loader import parse_allowed_dsps

    out: dict[str, str] = {}
    for code in parse_allowed_dsps(os.environ.get("ALLOWED_DSPS", "")):
        raw = (os.environ.get(f"DSP_ID_{code}", "") or "").strip()
        # A cell may carry several ids separated by ;/, — the console
        # companyId is the first one (same convention as horarios).
        first = next((p.strip() for p in raw.replace(";", ",").split(",") if p.strip()), "")
        if first:
            out[code] = first
    return out


def _read_cookies() -> dict[str, str]:
    try:
        return logistics_cookies_from_profile(target_host=DSP_CONSOLE_HOST)
    except Exception:  # noqa: BLE001
        return {}


def login_status(station: str = "") -> dict[str, Any]:
    """Session probe for the connections chip: ``{ready, expired, login_url}``.

    Pure cookie check (no network round-trip) — the same
    ``logistics-federate-token`` inspection Cortex uses, so the chip can
    be re-polled from the reconnect loop without hammering the console.
    """
    status = session_status(
        LOGISTICS_HOST_PATTERNS,
        LOGISTICS_REQUIRED_COOKIES,
        target_host=DSP_CONSOLE_HOST,
    )
    return {
        "ready": bool(status.get("ready")),
        "expired": status.get("expired") or [],
        "token_expired": "logistics-federate-token" in (status.get("expired") or []),
        "login_url": build_console_url(station),
    }


def _parse_summary_rows(payload: Any) -> dict[str, Any] | None:
    """First decoded row of ``dsp-station-vehicle-summaries`` (or None)."""
    if not isinstance(payload, dict):
        return None
    table = (payload.get("tableData") or {}).get("dsp-station-vehicle-summaries") or {}
    for raw in table.get("rows") or []:
        try:
            row = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            return row
    return None


def _opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def summary_from_row(row: dict[str, Any]) -> dict[str, int | None]:
    """Decoded summary row → fleet figures for the Fleet Real seed.

    ``real_capacity`` = ``active_vehicles`` − :data:`BACKUP_VANS` (floor 0)
    — the figure that lands in the Flota Total; the raw counters travel
    alongside for reference.
    """
    active = _opt_int(row.get("active_vehicles"))
    return {
        "total": _opt_int(row.get("registered_vehicles")),
        "active": active,
        "grounded": _opt_int(row.get("grounded_vehicles")),
        "out_of_service": _opt_int(row.get("out_of_service_vehicles")),
        "real_capacity": max(active - BACKUP_VANS, 0) if active is not None else None,
    }


def fetch_vehicle_summary(
    dsp_id: str,
    service_area_id: str,
    *,
    session: requests.Session,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, int | None]:
    """Fleet summary for one DSP: ``{total, active, grounded, out_of_service}``.

    ``total`` == ``registered_vehicles`` (the console's total fleet).
    Raises :class:`DspConsoleAuthError` on 401/403/redirect and
    :class:`DspConsoleError` on any other failure.
    """
    today = date.today()
    params = {
        "dspId": dsp_id,
        "from": today.isoformat(),
        "serviceAreaId": service_area_id,
        "to": (today + timedelta(days=_WINDOW_DAYS)).isoformat(),
    }
    url = f"{DSP_CONSOLE_BASE_URL}{_VEHICLE_SUMMARIES_PATH}?{urlencode(params)}"
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Referer": build_console_url(dsp_id=dsp_id),
    }
    try:
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise DspConsoleError(f"Error de red contra {DSP_CONSOLE_HOST}: {exc}") from exc

    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
        raise DspConsoleAuthError(
            f"El DSP console rechazo la peticion (HTTP {resp.status_code}). "
            "Inicia sesion en logistics.amazon.es en Firefox."
        )
    if resp.status_code != 200:
        raise DspConsoleError(
            f"DSP console devolvio HTTP {resp.status_code}. {(resp.text or '')[:200]}".strip()
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise DspConsoleError(f"DSP console no devolvio JSON valido: {exc}") from exc

    return summary_from_row(_parse_summary_rows(payload) or {})


def fetch_fleet_totals(
    station: str | None = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fleet totals for every configured DSP of the station.

    Returns ``{"station": code, "fleet": {dsp: {total, active, grounded,
    out_of_service}}, "fetched_at": iso}``. DSPs whose individual fetch
    fails (non-auth) are silently skipped — a partial answer is still
    useful for the Fleet Real seed. Raises :class:`DspConsoleAuthError`
    when the session is missing/rejected and :class:`DspConsoleError`
    when nothing could be fetched at all.
    """
    from datetime import datetime

    from app.integrations.cortex_client import resolve_service_area_id

    code = (station or "").strip().upper()
    service_area_id = resolve_service_area_id(code)
    if not service_area_id:
        raise DspConsoleError(
            "Falta CORTEX_SERVICE_AREA_ID en Identidad (identifica la DS "
            "en logistics.amazon.es)."
        )
    dsp_ids = configured_dsp_ids()
    if not dsp_ids:
        return {"station": code, "fleet": {}, "fetched_at": datetime.now().isoformat()}

    cookies = _read_cookies()
    if not has_required(cookies, LOGISTICS_REQUIRED_COOKIES):
        raise DspConsoleAuthError(
            f"Faltan cookies de {DSP_CONSOLE_HOST} (logistics-federate-token). "
            "Abre el DSP console en Firefox e inicia sesion."
        )
    session = requests.Session()
    session.cookies.update(cookies)

    fleet: dict[str, dict[str, int | None]] = {}
    errors: list[Exception] = []

    def _one(item: tuple[str, str]) -> None:
        dsp, dsp_id = item
        try:
            fleet[dsp] = fetch_vehicle_summary(
                dsp_id, service_area_id, session=session, timeout=timeout
            )
        except DspConsoleAuthError as exc:
            errors.append(exc)
        except DspConsoleError as exc:  # partial: skip this DSP only
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=min(4, len(dsp_ids))) as pool:
        list(pool.map(_one, dsp_ids.items()))

    if not fleet and errors:
        auth = next((e for e in errors if isinstance(e, DspConsoleAuthError)), None)
        if auth is not None:
            raise auth
        raise errors[0]

    return {"station": code, "fleet": fleet, "fetched_at": datetime.now().isoformat()}


__all__ = [
    "DspConsoleAuthError",
    "DspConsoleError",
    "DSP_CONSOLE_HOST",
    "BACKUP_VANS",
    "build_console_url",
    "configured_dsp_ids",
    "fetch_vehicle_summary",
    "fetch_fleet_totals",
    "login_status",
    "summary_from_row",
]
