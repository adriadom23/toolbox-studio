"""Roster View (``logistics.amazon.es``) work-schedules client.

Pulls the planned roster (each DA's transporter id + shift start time) that
the Roster View page shows, so the Wave Plan tool can auto-fill the roster
box instead of the operator pasting it by hand.

Discovered endpoint (same Federate session cookies as Cortex, no HMAC):

    GET /internal/capacity/rosterview/api/getWorkSchedules
        ?serviceAreaId=<uuid>&date=YYYY-MM-DD
        &timeWindowStartTime=<ms>&timeWindowEndTime=<ms>
        &filter={"fullTimeWindow":false,"isEndingInTimeWindow":false,
                 "isStartingInTimeWindow":true}

Response: a JSON list of work-schedule entries::

    {"durationInMinutes": 540, "startTime": 1781596800000,
     "person": {"id": "AP3D04IHNPKHB", "firstName": "...", ...},
     "transporterSession": {"scheduledStartTime": ..., ...}}

The ``serviceAreaId`` identifies the DS and is the same one configured for
Cortex (``CORTEX_SERVICE_AREA_ID``).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.integrations.firefox_cookies import (
    LOGISTICS_HOST_PATTERNS,
    LOGISTICS_REQUIRED_COOKIES,
    has_required,
    logistics_cookies_from_profile,
    session_status,
)


ROSTERVIEW_BASE_URL = "https://logistics.amazon.es"
ROSTERVIEW_PAGE_PATH = "/internal/capacity/rosterview"
ROSTERVIEW_API_PATH = "/internal/capacity/rosterview/api/getWorkSchedules"
ROSTERVIEW_TIMEZONE = "Europe/Madrid"
ROSTERVIEW_DEFAULT_TIMEOUT = 45.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)


class RosterViewAuthError(RuntimeError):
    """Raised when cookies are missing/expired (login required)."""


class RosterViewError(RuntimeError):
    """Raised on any other unexpected Roster View response."""


def _read_cookies() -> dict[str, str]:
    try:
        return logistics_cookies_from_profile()
    except Exception:  # noqa: BLE001
        return {}


def _session_fingerprint(cookies: dict[str, str]) -> str:
    from app.integrations.live_session_probe import cookie_fingerprint

    return cookie_fingerprint(cookies.get("logistics-federate-token"))


def _record_session_outcome(cookies: dict[str, str], outcome: str) -> None:
    """Remember auth observed by a real roster request for the status probe."""
    try:
        from app.integrations.live_session_probe import record_outcome

        record_outcome("rosterview", _session_fingerprint(cookies), outcome)
    except Exception:  # noqa: BLE001 - best-effort cache seed
        pass


def _probe_session(cookies: dict[str, str]) -> str:
    """Validate the Logistics session without downloading roster data."""
    session = requests.Session()
    session.cookies.update(cookies)
    try:
        page = session.get(
            ROSTERVIEW_BASE_URL + ROSTERVIEW_PAGE_PATH,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
            timeout=15.0,
            allow_redirects=False,
        )
    except requests.RequestException:
        return "error"
    if page.status_code in (301, 302, 303, 307, 308, 401, 403):
        return "auth"
    if page.status_code != 200:
        return "error"
    return "ok"


def login_status() -> dict[str, Any]:
    """Return the real Firefox session status for Roster View.

    Passive cookie/JWT checks run first. When they pass, a cached request to
    the Roster View page verifies that Logistics still accepts the session;
    this catches server-side invalidation without downloading roster data or
    hammering the page from the reconnect loop.
    """
    from app.integrations.live_session_probe import cached_probe

    status = session_status(
        LOGISTICS_HOST_PATTERNS,
        LOGISTICS_REQUIRED_COOKIES,
        target_host="logistics.amazon.es",
    )
    expired = status.get("expired") or []
    ready = bool(status.get("ready"))
    token_expired = "logistics-federate-token" in expired
    session_rejected = False
    reason = ""
    if ready:
        cookies = _read_cookies()
        outcome = cached_probe(
            "rosterview",
            _session_fingerprint(cookies),
            lambda: _probe_session(cookies),
        )
        if outcome == "auth":
            ready = False
            token_expired = True
            session_rejected = True
            reason = "session_rejected"
        elif outcome != "ok":
            ready = False
            reason = "network_error"
    return {
        "ready": ready,
        "missing": status.get("missing") or [],
        "expired": expired,
        "token_expired": token_expired,
        "session_rejected": session_rejected,
        "reason": reason,
        "cookie_count": status.get("cookie_count", 0),
        "login_url": ROSTERVIEW_BASE_URL + ROSTERVIEW_PAGE_PATH,
    }


def _normalize_date(value: str | datetime | None) -> str:
    if value is None:
        return datetime.now().date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Fecha no parseable: {value!r}")


def _day_window_ms(iso_date: str) -> tuple[int, int]:
    """Full-day window (with buffer) in epoch millis, anchored to Madrid."""
    tz = ZoneInfo(ROSTERVIEW_TIMEZONE)
    day = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=tz)
    start = day - timedelta(hours=6)
    end = day + timedelta(hours=30)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _ms_to_hhmm(value: Any) -> str:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=ZoneInfo(ROSTERVIEW_TIMEZONE))
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.strftime("%H:%M")


def _clean_da_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()[:14]


def fetch_work_schedules(
    service_area_id: str,
    date: str | datetime | None = None,
    *,
    cookies: dict[str, str] | None = None,
    timeout: float = ROSTERVIEW_DEFAULT_TIMEOUT,
) -> list[dict[str, str]]:
    """Return the roster as ``[{"da_id", "hora_inicio"}]`` for one DS/date.

    Raises :class:`RosterViewAuthError` when login is required and
    :class:`RosterViewError` for any other failure.
    """
    sa_id = (service_area_id or "").strip()
    if not sa_id:
        raise RosterViewError("Falta serviceAreaId para el Roster View.")
    iso_date = _normalize_date(date)

    if cookies is None:
        cookies = _read_cookies()
    if not has_required(cookies, LOGISTICS_REQUIRED_COOKIES):
        raise RosterViewAuthError(
            "Faltan cookies de logistics.amazon.es (logistics-federate-token). "
            "Abre el Roster View en Firefox e inicia sesion."
        )

    start_ms, end_ms = _day_window_ms(iso_date)
    params = {
        "serviceAreaId": sa_id,
        "date": iso_date,
        "timeWindowStartTime": start_ms,
        "timeWindowEndTime": end_ms,
        "filter": json.dumps(
            {
                "fullTimeWindow": False,
                "isEndingInTimeWindow": False,
                "isStartingInTimeWindow": True,
            }
        ),
    }
    page_url = f"{ROSTERVIEW_BASE_URL}{ROSTERVIEW_PAGE_PATH}?serviceAreaId={sa_id}&date={iso_date}"
    session = requests.Session()
    session.cookies.update(cookies)
    try:
        resp = session.get(
            f"{ROSTERVIEW_BASE_URL}{ROSTERVIEW_API_PATH}",
            params=params,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Referer": page_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.SSLError as exc:
        raise RosterViewError(f"Error SSL contra logistics.amazon.es: {exc}") from exc
    except requests.RequestException as exc:
        raise RosterViewError(f"Error de red contra Roster View: {exc}") from exc

    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
        _record_session_outcome(cookies, "auth")
        raise RosterViewAuthError(
            f"Roster View rechazo la peticion (HTTP {resp.status_code}). "
            "Recarga la pagina en Firefox e inicia sesion."
        )
    if resp.status_code != 200:
        raise RosterViewError(
            f"Roster View devolvio HTTP {resp.status_code}. {(resp.text or '')[:200]}".strip()
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RosterViewError(f"Roster View no devolvio JSON valido: {exc}") from exc
    _record_session_outcome(cookies, "ok")

    entries = data if isinstance(data, list) else (data.get("workSchedules") or [])
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        person = entry.get("person") or {}
        da_id = _clean_da_id(person.get("id") or person.get("fullName") or "")
        if not da_id or da_id in seen:
            continue
        seen.add(da_id)
        hora = _ms_to_hhmm(
            entry.get("startTime")
            or (entry.get("transporterSession") or {}).get("scheduledStartTime")
        )
        # DSP short code (JSIS / CDOR / TOGO ...) — the company's shortCode.
        # IHS stores have an empty shortCode; they keep "" so they group apart.
        company = entry.get("company") or {}
        dsp = str(company.get("shortCode") or "").strip().upper()
        rows.append({"da_id": da_id, "hora_inicio": hora, "dsp": dsp})
    return rows


def _schedule_window(entry: dict[str, Any]) -> tuple[str, str, int]:
    """Return ``(shift_start, shift_end, minutes)`` from an entry.

    Reads ``startTime`` (epoch ms) + ``durationInMinutes`` defensively and
    falls back to the ``transporterSession.scheduledStartTime`` the same way
    :func:`fetch_work_schedules` does.
    """
    raw_start = entry.get("startTime") or (
        entry.get("transporterSession") or {}
    ).get("scheduledStartTime")
    shift_start = _ms_to_hhmm(raw_start)
    minutes = 0
    try:
        minutes = int(entry.get("durationInMinutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    shift_end = ""
    if shift_start and minutes > 0:
        try:
            start_ms = int(raw_start)
            shift_end = _ms_to_hhmm(start_ms + minutes * 60_000)
        except (TypeError, ValueError):
            shift_end = ""
    return shift_start, shift_end, max(minutes, 0)


def fetch_work_schedule_details(
    service_area_id: str,
    date: str | datetime | None = None,
    *,
    cookies: dict[str, str] | None = None,
    timeout: float = ROSTERVIEW_DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the FULL per-schedule detail for one DS/date (no DA dedupe).

    Unlike :func:`fetch_work_schedules` (which collapses to one row per DA),
    this preserves every schedule entry — including several schedules for the
    same ``person.id`` on the same day — so the Nursery Checker Dive Deep can
    rebuild a driver's day-by-day Service Type history. Each row carries::

        {"driver_id", "driver_name", "dsp", "company_name", "service_type",
         "nursery_level", "nursery_display", "availability", "transport_mode",
         "shift_start", "shift_end", "shift_minutes", "cycle"}

    Same GET/params/auth as :func:`fetch_work_schedules` (verified contract);
    pass ``cookies`` to reuse a single jar across the 60 daily calls. Raises
    :class:`RosterViewAuthError` when login is required and
    :class:`RosterViewError` for any other failure.
    """
    sa_id = (service_area_id or "").strip()
    if not sa_id:
        raise RosterViewError("Falta serviceAreaId para el Roster View.")
    iso_date = _normalize_date(date)

    if cookies is None:
        cookies = _read_cookies()
    if not has_required(cookies, LOGISTICS_REQUIRED_COOKIES):
        raise RosterViewAuthError(
            "Faltan cookies de logistics.amazon.es (logistics-federate-token). "
            "Abre el Roster View en Firefox e inicia sesion."
        )

    start_ms, end_ms = _day_window_ms(iso_date)
    params = {
        "serviceAreaId": sa_id,
        "date": iso_date,
        "timeWindowStartTime": start_ms,
        "timeWindowEndTime": end_ms,
        "filter": json.dumps(
            {
                "fullTimeWindow": False,
                "isEndingInTimeWindow": False,
                "isStartingInTimeWindow": True,
            }
        ),
    }
    page_url = f"{ROSTERVIEW_BASE_URL}{ROSTERVIEW_PAGE_PATH}?serviceAreaId={sa_id}&date={iso_date}"
    session = requests.Session()
    session.cookies.update(cookies)
    try:
        resp = session.get(
            f"{ROSTERVIEW_BASE_URL}{ROSTERVIEW_API_PATH}",
            params=params,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Referer": page_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.SSLError as exc:
        raise RosterViewError(f"Error SSL contra logistics.amazon.es: {exc}") from exc
    except requests.RequestException as exc:
        raise RosterViewError(f"Error de red contra Roster View: {exc}") from exc

    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
        _record_session_outcome(cookies, "auth")
        raise RosterViewAuthError(
            f"Roster View rechazo la peticion (HTTP {resp.status_code}). "
            "Recarga la pagina en Firefox e inicia sesion."
        )
    if resp.status_code != 200:
        raise RosterViewError(
            f"Roster View devolvio HTTP {resp.status_code}. {(resp.text or '')[:200]}".strip()
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RosterViewError(f"Roster View no devolvio JSON valido: {exc}") from exc
    _record_session_outcome(cookies, "ok")

    entries = data if isinstance(data, list) else (data.get("workSchedules") or [])
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        person = entry.get("person") or {}
        driver_id = _clean_da_id(person.get("id") or person.get("fullName") or "")
        if not driver_id:
            continue
        name = str(person.get("fullName") or "").strip()
        if not name:
            first = str(person.get("firstName") or "").strip()
            last = str(person.get("lastName") or "").strip()
            name = " ".join(part for part in (first, last) if part)
        company = entry.get("company") or {}
        dsp = str(company.get("shortCode") or "").strip().upper()
        company_name = str(company.get("name") or "").strip()
        service_type = str(entry.get("serviceTypeName") or "").strip()
        nursery = entry.get("nurseryInformation") or {}
        nursery_level = str(nursery.get("nurseryLevel") or "").strip()
        nursery_display = bool(nursery.get("shouldDisplay"))
        shift_start, shift_end, minutes = _schedule_window(entry)
        rows.append(
            {
                "driver_id": driver_id,
                "driver_name": name,
                "dsp": dsp,
                "company_name": company_name,
                "service_type": service_type,
                "nursery_level": nursery_level,
                "nursery_display": nursery_display,
                "availability": str(entry.get("availability") or "").strip(),
                "transport_mode": str(
                    entry.get("transportMode")
                    or entry.get("transportationMode")
                    or (entry.get("transporterSession") or {}).get("transportMode")
                    or ""
                ).strip(),
                "shift_start": shift_start,
                "shift_end": shift_end,
                "shift_minutes": minutes,
                "cycle": str(entry.get("cycleName") or "").strip(),
            }
        )
    return rows


def build_roster_text(rows: list[dict[str, str]]) -> str:
    """Render fetched rows as the tab-separated text the paste box accepts.

    Includes the DSP (short code) as a trailing ``Empresa`` column so the
    crossmatch can group by DSP straight from the Roster View.
    """
    lines = ["Identificador DA\tHora de inicio\tEmpresa"]
    for row in rows:
        lines.append(
            f"{row.get('da_id', '')}\t{row.get('hora_inicio', '')}\t{row.get('dsp', '')}"
        )
    return "\n".join(lines)


__all__ = [
    "RosterViewAuthError",
    "RosterViewError",
    "fetch_work_schedules",
    "fetch_work_schedule_details",
    "build_roster_text",
    "login_status",
]
