"""Cortex (``logistics.amazon.co.uk``) itineraries client.

Replaces the manual "Itinerarios de Cortex" Excel download used by the
Flow Meeting tool with a direct, signed API call:

    GET https://logistics.amazon.co.uk/internal/operations/execution/api/summaries
        ?historicalDay=false&localDate=YYYY-MM-DD&serviceAreaId=<uuid>

The endpoint is protected by a per-page-load HMAC request signature
(``X-Cortex-*`` headers) on top of the Midway/Federate session cookies.
We reproduce the browser's signing scheme:

1. Read ``logistics-federate-token`` (+ session cookies) from the user's
   Firefox profile.
2. GET the itineraries HTML page and scrape the embedded
   ``hmacSession`` ({sessionKey, sessionId}) from the
   ``<script type="a-state" data-a-state='{"key":"hmacSession"}'>`` block.
3. Build the SigV4-style canonical string and sign it with
   HMAC-SHA256 (key = the base64 ``sessionKey`` string used as raw UTF-8
   bytes, output base64).
4. Call ``/api/summaries`` with the signed ``X-Cortex-*`` headers.

The ``serviceAreaId`` identifies the DS and is configured per station
code (Identidad -> ``CORTEX_SERVICE_AREA_ID``).

Public surface:

* :class:`CortexAuthError` / :class:`CortexClientError`
* :func:`cookies_status`
* :func:`fetch_summaries` — raw decoded JSON for one service area.
* :func:`fetch_itinerarios_df` — normalised DataFrame matching the
  output schema of :func:`app.tools.common.flow_meeting.load_itinerarios`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime
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


CORTEX_HOST = "logistics.amazon.co.uk"
CORTEX_BASE_URL = f"https://{CORTEX_HOST}"
CORTEX_PROVIDER = "ALL_DRIVERS"
CORTEX_SUMMARIES_PATH = "/internal/operations/execution/api/summaries"
# Richer per-route endpoint used by the itineraries page (carries route type,
# duration, stops/packages and the transporters assigned to each route). Only
# the cierre diagnostic uses it for now — see ``fetch_route_summaries``.
CORTEX_ROUTE_SUMMARIES_PATH = "/internal/operations/execution/api/route-summaries"
CORTEX_PAGE_PATH = "/internal/operations/execution/itineraries"
# Compatibility fallback for callers that do not yet have an area/date. Normal
# authentication URLs should be built with ``build_cortex_page_url``.
CORTEX_LOGIN_URL = CORTEX_BASE_URL + CORTEX_PAGE_PATH
CORTEX_DEFAULT_TIMEOUT = 40.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# Signed headers, in the order the canonical string declares them.
_SIGNED_HEADERS = "x-cortex-session;x-cortex-timestamp"


class CortexAuthError(RuntimeError):
    """Raised when cookies/HMAC are missing or rejected (login required)."""


class CortexClientError(RuntimeError):
    """Raised on any other unexpected Cortex response."""


# ---------------------------------------------------------------------------
# Cookies / login status
# ---------------------------------------------------------------------------


def _read_cookies() -> dict[str, str]:
    try:
        return logistics_cookies_from_profile(target_host=CORTEX_HOST)
    except Exception:  # noqa: BLE001
        return {}


def _session_fingerprint(cookies: dict[str, str]) -> str:
    from app.integrations.live_session_probe import cookie_fingerprint

    return cookie_fingerprint(cookies.get("logistics-federate-token"))


def _record_session_outcome(cookies: dict[str, str], outcome: str) -> None:
    """Remember auth observed by a real extraction for the status probe."""
    try:
        from app.integrations.live_session_probe import record_outcome

        record_outcome("cortex", _session_fingerprint(cookies), outcome)
    except Exception:  # noqa: BLE001 - best-effort cache seed
        pass


def _probe_session(cookies: dict[str, str], service_area_id: str) -> str:
    """Check the same itineraries-page/HMAC step required by extraction."""
    session = requests.Session()
    session.cookies.update(cookies)
    try:
        page = session.get(
            build_cortex_page_url(service_area_id),
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
    return "ok" if _scrape_hmac_session(page.text or "") else "auth"


def cookies_status() -> dict[str, Any]:
    """Return live Cortex readiness plus passive cookie diagnostics.

    A locally unexpired ``logistics-federate-token`` is not sufficient: Cortex
    can reject it server-side. When passive checks pass, validate the same
    itineraries-page/HMAC step extraction needs. Transport and 5xx failures keep
    the passive verdict to avoid connection-chip flapping.
    """
    from app.integrations.live_session_probe import cached_probe

    status = session_status(
        LOGISTICS_HOST_PATTERNS,
        LOGISTICS_REQUIRED_COOKIES,
        target_host=CORTEX_HOST,
    )
    areas = configured_areas()
    area_id = next(iter(areas.values()), "")
    login_url = build_cortex_page_url(area_id) if area_id else CORTEX_LOGIN_URL
    ready = bool(status["ready"])
    token_expired = "logistics-federate-token" in status["expired"]
    session_rejected = False
    if ready and area_id:
        cookies = _read_cookies()
        outcome = cached_probe(
            "cortex",
            _session_fingerprint(cookies),
            lambda: _probe_session(cookies, area_id),
        )
        if outcome == "auth":
            ready = False
            token_expired = True
            session_rejected = True
    return {
        "ready": ready,
        "missing": status["missing"],
        "expired": status["expired"],
        "token_expired": token_expired,
        "session_rejected": session_rejected,
        "cookie_count": status["cookie_count"],
        "login_url": login_url,
    }


def resolve_service_area_id(station_code: str | None = None) -> str:
    """Return the configured Cortex ``serviceAreaId`` for a station code.

    Resolution order:
      1. ``CORTEX_SERVICE_AREA_ID_<CODE>`` (per-code override, for DSs that
         share two station codes via ``DOUBLESTATION_CODE``).
      2. ``CORTEX_SERVICE_AREA_ID`` (single-station default).
    """
    import os

    code = (station_code or "").strip().upper()
    if code:
        scoped = os.environ.get(f"CORTEX_SERVICE_AREA_ID_{code}", "").strip()
        if scoped:
            return scoped
    return os.environ.get("CORTEX_SERVICE_AREA_ID", "").strip()


def configured_station_codes() -> list[str]:
    """Station codes to fetch: primary ``STATION_CODE`` + ``DOUBLESTATION_CODE``."""
    import os
    import re

    primary = (os.environ.get("STATION_CODE", "") or "").strip().upper()
    extra = (
        os.environ.get("DOUBLESTATION_CODE", "")
        or os.environ.get("STATION_CODES", "")
    ).strip()
    codes: list[str] = []
    for token in re.split(r"[;,]+", extra):
        cleaned = token.strip().upper()
        if cleaned and cleaned not in codes:
            codes.append(cleaned)
    if primary and primary not in codes:
        codes.append(primary)
    return codes


def configured_areas() -> dict[str, str]:
    """Return ``{station_code: serviceAreaId}`` for every configured area.

    Honours the per-code keys (``CORTEX_SERVICE_AREA_ID_<CODE>``) and the
    base ``CORTEX_SERVICE_AREA_ID`` fallback. Used by the status endpoint
    and the auto-fetch so both agree on what is configured.
    """
    import os

    out: dict[str, str] = {}
    codes = configured_station_codes()
    if not codes:
        base = os.environ.get("CORTEX_SERVICE_AREA_ID", "").strip()
        if base:
            out[""] = base
        return out
    for code in codes:
        sa_id = resolve_service_area_id(code)
        if sa_id:
            out[code] = sa_id
    return out


# ---------------------------------------------------------------------------
# HMAC request signing
# ---------------------------------------------------------------------------


_HMAC_STATE_RE = re.compile(
    r'data-a-state="([^"]*hmacSession[^"]*)"\s*>(.*?)</script>',
    re.DOTALL,
)
_HMAC_JSON_RE = re.compile(r'\{"hmacSession":\{.*?\}\}')


def _scrape_hmac_session(html_text: str) -> dict[str, Any] | None:
    """Extract ``{sessionKey, sessionId, ...}`` from the page HTML."""
    for match in _HMAC_STATE_RE.finditer(html_text):
        try:
            data = json.loads(match.group(2).strip())
        except Exception:  # noqa: BLE001
            continue
        sess = data.get("hmacSession")
        if isinstance(sess, dict) and sess.get("sessionKey") and sess.get("sessionId"):
            return sess
    match = _HMAC_JSON_RE.search(html_text)
    if match:
        try:
            sess = json.loads(match.group(0)).get("hmacSession")
            if isinstance(sess, dict) and sess.get("sessionKey"):
                return sess
        except Exception:  # noqa: BLE001
            return None
    return None


def _canonical_string(method: str, path: str, params: dict[str, str], session_id: str, ts: str) -> str:
    # Mirrors the webapp: sort query keys, re-encode, then the SigV4-like
    # block of method/path/query/signed-headers.
    from urllib.parse import urlencode

    query = urlencode(sorted(params.items(), key=lambda kv: kv[0]))
    return "\n".join(
        [
            method.upper(),
            path,
            query,
            f"x-cortex-session:{session_id}",
            f"x-cortex-timestamp:{ts}",
            "",
            _SIGNED_HEADERS,
        ]
    )


def _sign(canonical: str, session_key: str) -> str:
    # The webapp uses the base64 ``sessionKey`` string as the raw HMAC key
    # (NOT base64-decoded); SHA-256; output base64.
    digest = hmac.new(
        session_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _normalize_local_date(local_date: date | datetime | str | None) -> str:
    if local_date is None:
        return date.today().isoformat()
    if isinstance(local_date, datetime):
        return local_date.date().isoformat()
    if isinstance(local_date, date):
        return local_date.isoformat()
    text = str(local_date).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"local_date no parseable: {local_date!r}")


def build_cortex_page_url(
    service_area_id: str,
    local_date: date | datetime | str | None = None,
) -> str:
    """Build the Cortex authentication page URL for one area and day."""
    sa_id = (service_area_id or "").strip()
    if not sa_id:
        raise ValueError("Falta serviceAreaId para construir la URL de Cortex.")
    params = {
        "provider": CORTEX_PROVIDER,
        "selectedDay": _normalize_local_date(local_date),
        "serviceAreaId": sa_id,
    }
    return f"{CORTEX_BASE_URL}{CORTEX_PAGE_PATH}?{urlencode(params)}"


def fetch_summaries(
    service_area_id: str,
    local_date: date | datetime | str | None = None,
    *,
    historical: bool = False,
    cookies: dict[str, str] | None = None,
    timeout: float = CORTEX_DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch and return the decoded ``/api/summaries`` JSON for one DS.

    Raises :class:`CortexAuthError` when login is required and
    :class:`CortexClientError` for any other failure.
    """
    sa_id = (service_area_id or "").strip()
    if not sa_id:
        raise CortexClientError("Falta serviceAreaId para Cortex.")
    iso_date = _normalize_local_date(local_date)

    if cookies is None:
        cookies = _read_cookies()
    if not has_required(cookies, LOGISTICS_REQUIRED_COOKIES):
        raise CortexAuthError(
            f"Faltan cookies de {CORTEX_HOST} (logistics-federate-token). "
            "Abre Cortex en Firefox e inicia sesion."
        )

    session = requests.Session()
    session.cookies.update(cookies)

    # 1) Fresh hmacSession from the itineraries page HTML.
    page_url = build_cortex_page_url(sa_id, iso_date)
    try:
        page = session.get(
            page_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.SSLError as exc:
        raise CortexClientError(f"Error SSL contra {CORTEX_HOST}: {exc}") from exc
    except requests.RequestException as exc:
        raise CortexClientError(f"Error de red contra {CORTEX_HOST}: {exc}") from exc

    if page.status_code in (301, 302, 303, 307, 308) or page.status_code in (401, 403):
        _record_session_outcome(cookies, "auth")
        raise CortexAuthError(
            f"Cortex redirige/rechaza la pagina (HTTP {page.status_code}). "
            "Recarga Cortex en Firefox e inicia sesion."
        )
    if page.status_code != 200:
        raise CortexClientError(f"Cortex devolvio HTTP {page.status_code} al cargar la pagina.")

    sess = _scrape_hmac_session(page.text or "")
    if not sess:
        _record_session_outcome(cookies, "auth")
        raise CortexAuthError(
            "No se encontro la sesion HMAC en Cortex (sesion caducada). "
            "Recarga Cortex en Firefox."
        )

    # 2) Sign and call the summaries API.
    ts = str(int(time.time() * 1000))
    params = {
        "historicalDay": "true" if historical else "false",
        "localDate": iso_date,
        "serviceAreaId": sa_id,
    }
    canonical = _canonical_string("GET", CORTEX_SUMMARIES_PATH, params, sess["sessionId"], ts)
    signature = _sign(canonical, sess["sessionKey"])

    from urllib.parse import urlencode

    url = f"{CORTEX_BASE_URL}{CORTEX_SUMMARIES_PATH}?" + urlencode(params)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "user-ref": "cortex-webapp-user",
        "X-Cortex-Hmac-Signature": signature,
        "X-Cortex-Timestamp": ts,
        "X-Cortex-Session": sess["sessionId"],
        "Referer": page_url,
    }
    try:
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=False)
    except requests.exceptions.SSLError as exc:
        raise CortexClientError(f"Error SSL contra Cortex API: {exc}") from exc
    except requests.RequestException as exc:
        raise CortexClientError(f"Error de red contra Cortex API: {exc}") from exc

    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
        _record_session_outcome(cookies, "auth")
        raise CortexAuthError(
            f"Cortex rechazo la peticion ({resp.status_code}). "
            "Recarga Cortex en Firefox e intenta de nuevo."
        )
    if resp.status_code != 200:
        raise CortexClientError(
            f"Cortex API devolvio HTTP {resp.status_code}. {(resp.text or '')[:200]}".strip()
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise CortexClientError(f"Cortex API no devolvio JSON valido: {exc}") from exc
    _record_session_outcome(cookies, "ok")
    return data


def fetch_route_summaries(
    service_area_id: str,
    local_date: date | datetime | str | None = None,
    *,
    historical: bool = False,
    cookies: dict[str, str] | None = None,
    timeout: float = CORTEX_DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch the decoded ``/api/route-summaries`` JSON for one DS.

    This is the richer per-route endpoint the itineraries page uses. Unlike
    :func:`fetch_summaries` it exposes ``serviceTypeName`` (route type),
    ``routeDuration`` and, per route, the assigned ``transporters`` with their
    ``routeDeliveryProgress`` (stops/packages). Signing scheme and auth are
    identical to :func:`fetch_summaries`; the only differences are the path
    and the extra ``statsFromSummaries=true`` query param.

    Kept separate (additive) so the shared auto-fetch used by other tools is
    unaffected — only the cierre diagnostic consumes it for now.

    Raises :class:`CortexAuthError` when login is required and
    :class:`CortexClientError` for any other failure.
    """
    sa_id = (service_area_id or "").strip()
    if not sa_id:
        raise CortexClientError("Falta serviceAreaId para Cortex.")
    iso_date = _normalize_local_date(local_date)

    if cookies is None:
        cookies = _read_cookies()
    if not has_required(cookies, LOGISTICS_REQUIRED_COOKIES):
        raise CortexAuthError(
            f"Faltan cookies de {CORTEX_HOST} (logistics-federate-token). "
            "Abre Cortex en Firefox e inicia sesion."
        )

    session = requests.Session()
    session.cookies.update(cookies)

    # 1) Fresh hmacSession from the itineraries page HTML.
    page_url = build_cortex_page_url(sa_id, iso_date)
    try:
        page = session.get(
            page_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.SSLError as exc:
        raise CortexClientError(f"Error SSL contra {CORTEX_HOST}: {exc}") from exc
    except requests.RequestException as exc:
        raise CortexClientError(f"Error de red contra {CORTEX_HOST}: {exc}") from exc

    if page.status_code in (301, 302, 303, 307, 308, 401, 403):
        _record_session_outcome(cookies, "auth")
        raise CortexAuthError(
            f"Cortex redirige/rechaza la pagina (HTTP {page.status_code}). "
            "Recarga Cortex en Firefox e inicia sesion."
        )
    if page.status_code != 200:
        raise CortexClientError(f"Cortex devolvio HTTP {page.status_code} al cargar la pagina.")

    sess = _scrape_hmac_session(page.text or "")
    if not sess:
        _record_session_outcome(cookies, "auth")
        raise CortexAuthError(
            "No se encontro la sesion HMAC en Cortex (sesion caducada). "
            "Recarga Cortex en Firefox."
        )

    # 2) Sign and call the route-summaries API.
    ts = str(int(time.time() * 1000))
    params = {
        "historicalDay": "true" if historical else "false",
        "localDate": iso_date,
        "serviceAreaId": sa_id,
        "statsFromSummaries": "true",
    }
    canonical = _canonical_string(
        "GET", CORTEX_ROUTE_SUMMARIES_PATH, params, sess["sessionId"], ts
    )
    signature = _sign(canonical, sess["sessionKey"])

    url = f"{CORTEX_BASE_URL}{CORTEX_ROUTE_SUMMARIES_PATH}?" + urlencode(params)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "user-ref": "cortex-webapp-user",
        "X-Cortex-Hmac-Signature": signature,
        "X-Cortex-Timestamp": ts,
        "X-Cortex-Session": sess["sessionId"],
        "Referer": page_url,
    }
    try:
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=False)
    except requests.exceptions.SSLError as exc:
        raise CortexClientError(f"Error SSL contra Cortex API: {exc}") from exc
    except requests.RequestException as exc:
        raise CortexClientError(f"Error de red contra Cortex API: {exc}") from exc

    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
        _record_session_outcome(cookies, "auth")
        raise CortexAuthError(
            f"Cortex rechazo la peticion ({resp.status_code}). "
            "Recarga Cortex en Firefox e intenta de nuevo."
        )
    if resp.status_code != 200:
        raise CortexClientError(
            f"Cortex API devolvio HTTP {resp.status_code}. {(resp.text or '')[:200]}".strip()
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise CortexClientError(f"Cortex API no devolvio JSON valido: {exc}") from exc
    _record_session_outcome(cookies, "ok")
    return data


# ---------------------------------------------------------------------------
# Normalisation -> load_itinerarios output schema
# ---------------------------------------------------------------------------


def _epoch_to_dt(value: Any):
    """Epoch (ms or s, possibly float) -> naive datetime, else None."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    # Heuristic: >1e12 is milliseconds (and >1e15 is microseconds).
    if num > 1e14:
        num /= 1_000_000.0
    elif num > 1e11:
        num /= 1000.0
    try:
        return datetime.fromtimestamp(num)
    except (OverflowError, OSError, ValueError):
        return None


def summaries_to_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Map ``/api/summaries`` JSON to rows matching ``load_itinerarios``.

    Only DSP-operated itineraries are kept (IHS stores are dropped).
    """
    summaries = data.get("itinerarySummaries") or []
    companies = data.get("companies") or []
    company_by_id: dict[str, dict[str, Any]] = {}
    for company in companies:
        cid = str(company.get("companyId") or company.get("id") or "")
        if cid:
            company_by_id[cid] = company

    rows: list[dict[str, Any]] = []
    for summary in summaries:
        company = company_by_id.get(str(summary.get("companyId") or ""), {})
        company_type = str(company.get("companyType") or "").upper()
        # Keep DSP itineraries; skip in-home-services stores.
        if company_type and company_type != "DSP":
            continue
        dsp_name = str(company.get("companyName") or "").strip()

        tta = summary.get("transporterTimeAttributes") or {}
        stop_progress = summary.get("stopProgress") or {}

        ot_secs = summary.get("overtimeDurationSecs")
        predicted_ot_min = (ot_secs / 60.0) if isinstance(ot_secs, (int, float)) else None

        break_secs = summary.get("totalBreaksDurationSecs")
        break_min = (break_secs / 60.0) if isinstance(break_secs, (int, float)) else None

        # Average pace: completed stops over elapsed hours since departure.
        completed = stop_progress.get("completed")
        actual_dep = _epoch_to_dt(tta.get("actualDepartureTime"))
        pace = None
        if isinstance(completed, (int, float)) and completed > 0 and actual_dep is not None:
            elapsed_h = (datetime.now() - actual_dep).total_seconds() / 3600.0
            if elapsed_h > 0.05:
                pace = round(completed / elapsed_h, 2)

        route_code = str(summary.get("routeCode") or "").strip().upper()
        route_codes = [
            str(rc).strip().upper()
            for rc in (summary.get("routeCodes") or [])
            if str(rc).strip()
        ]
        if not route_codes and route_code:
            route_codes = [route_code]

        last_activity = _epoch_to_dt(
            summary.get("lastStopExecutionTime") or summary.get("lastDriverEventTime")
        )
        predicted_return = _epoch_to_dt(
            tta.get("projectedCompletionTime") or tta.get("plannedRtsTime")
        )

        rows.append(
            {
                "transporter_id": str(summary.get("transporterId") or "").strip(),
                "dsp": dsp_name,
                "route_code_raw": route_code,
                "predicted_return_time": predicted_return,
                "predicted_ot_min": predicted_ot_min,
                "stops_total": stop_progress.get("total"),
                "stops_completed": completed,
                "stops_not_started": stop_progress.get("notStarted"),
                "planned_pkgs": summary.get("totalPackages"),
                "pace_stops_per_h": pace,
                "login_time": _epoch_to_dt(
                    summary.get("itineraryStartTime") or tta.get("actualDepartureTime")
                ),
                "logout_time": "",
                "last_activity_time": last_activity,
                "break_time_min": break_min,
                "route_codes_list": route_codes,
                "is_multi_route": len(route_codes) > 1,
            }
        )
    return rows


def fetch_itinerarios_df(
    service_area_id: str,
    local_date: date | datetime | str | None = None,
    *,
    cookies: dict[str, str] | None = None,
):
    """Fetch Cortex itineraries and return a DataFrame matching the output
    schema of :func:`app.tools.common.flow_meeting.load_itinerarios`."""
    import pandas as pd

    data = fetch_summaries(service_area_id, local_date, cookies=cookies)
    records = summaries_to_records(data)
    columns = [
        "transporter_id", "dsp", "route_code_raw", "predicted_return_time",
        "predicted_ot_min", "stops_total", "stops_completed", "stops_not_started",
        "planned_pkgs", "pace_stops_per_h", "login_time", "logout_time",
        "last_activity_time", "break_time_min", "route_codes_list", "is_multi_route",
    ]
    if not records:
        return pd.DataFrame({c: [] for c in columns})
    df = pd.DataFrame(records)
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[columns]
    df = df.sort_values("last_activity_time", ascending=True)
    df = df.drop_duplicates(subset=["transporter_id"], keep="last")
    return df


__all__ = [
    "CortexAuthError",
    "CortexClientError",
    "CORTEX_HOST",
    "CORTEX_PROVIDER",
    "CORTEX_LOGIN_URL",
    "build_cortex_page_url",
    "cookies_status",
    "fetch_summaries",
    "fetch_route_summaries",
    "summaries_to_records",
    "fetch_itinerarios_df",
    "resolve_service_area_id",
    "configured_station_codes",
    "configured_areas",
]
