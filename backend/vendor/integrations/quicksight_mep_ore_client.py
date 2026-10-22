"""QuickSight MEP On-Road Experience table client.

Reads the verified ``Table 1.2: SIM Data for Open Cases`` visual from the
EU DSP MEP Central Dashboard. The query is station-scoped before it reaches
QuickSight's table row limit and never writes or sends anything.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import uuid
from typing import Any

import requests

from app.core.env_loader import parse_allowed_dsps
from app.integrations.quicksight_scheduling_client import (
    ALIAS,
    HOST,
    QuickSightAuthError,
    QuickSightError,
    _UA,
    _connect_ws,
    _extract_set_cookies,
    _read_cookies,
    _stream_vis,
    dashboard_login_status,
)

DASHBOARD_ID = "aa3fc89d-64e2-4276-a7d9-b7526d7849cf"
SHEET_ID = f"{DASHBOARD_ID}_af07af13-6264-4ea6-a6f8-12065df974d5"
VISUAL_ID = f"{DASHBOARD_ID}_bc11eec8-5f7c-497e-848f-40aafb97dba6"
STREAM_SOURCE_ID = "f34a7214-f499-4270-a535-89c28665042b"
LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"

_SOURCE = "1a290a2a-dba2-4948-a97e-73f00b01dfe5"
_FIELD_STATION = f"{_SOURCE}.station"
_FIELD_DSP = f"{_SOURCE}.dsp"
_FIELD_MEP_STATUS = f"{_SOURCE}.mep_status"
_FIELD_MEP_NAME = f"{_SOURCE}.mep_name"
# The visual's Master SIM column is a dashboard calculated field. The
# verified prepared source exposes its backing value as ``main_id``.
_FIELD_MASTER_SIM = f"{_SOURCE}.main_id"
_FIELD_STATUS = f"{_SOURCE}.status"
_GROUP_FIELDS = (
    _FIELD_STATION,
    _FIELD_DSP,
    _FIELD_MEP_STATUS,
    _FIELD_MEP_NAME,
    _FIELD_MASTER_SIM,
    _FIELD_STATUS,
)
# The visual configuration has no native data-reduction limit. Request the
# standard high table window so a station-scoped result is not silently cut at
# an arbitrary small page size. QuickSight can nevertheless return a single
# table frame capped at roughly 2,048 rows; callers expose that condition
# instead of treating a potentially partial result as complete.
REQUEST_ROW_LIMIT = 50_000
QUICKSIGHT_TABLE_FRAME_MAX_ROWS = 2_048
_STATION_CODE_RE = re.compile(r"^[A-Z0-9]{3,10}$")

# The table links Master SIM values only through the generic MEP/SIMTT portal;
# category-specific wiki URLs stay available as separate guidance links.
MEP_SIMTT_PORTAL_URL = "https://w.amazon.com/bin/view/EUCO/DriverSupportPortal/OnRoadExperience/What_is_an_MEP"
WIKI_PORTAL_URL = MEP_SIMTT_PORTAL_URL
_WIKI_BASE = "https://w.amazon.com/bin/view/EUCO/DriverSupportPortal/OnRoadExperience/"

_CATEGORY_URLS = {
    "dsc": f"{_WIKI_BASE}DSC_MEP",
    "dcr": f"{_WIKI_BASE}DCR_MEP",
    "capacity_reliability": f"{_WIKI_BASE}Capacity_Reliability_MEP",
    "doosd": f"{_WIKI_BASE}DOOSD_MEP",
    "dvic": f"{_WIKI_BASE}DIVC_MEP",
    "hv": f"{_WIKI_BASE}High_Value_MEP",
    "lor": f"{_WIKI_BASE}LOR_MEP",
    "pm": f"{_WIKI_BASE}PM_MEP",
    "safety_distraction": f"{_WIKI_BASE}On-Road_Safety_MEP",
    "safety_fico": f"{_WIKI_BASE}On-Road_Safety_MEP",
    "safety_following_distance": f"{_WIKI_BASE}On-Road_Safety_MEP",
    "safety_sign_signal_violations": f"{_WIKI_BASE}On-Road_Safety_MEP",
    "safety_speeding": f"{_WIKI_BASE}On-Road_Safety_MEP",
    "rental_debt": f"{_WIKI_BASE}Rental_Debt_MEP",
    "armada_debt": f"{_WIKI_BASE}Armada_Debt_MEP",
}


def _page_url() -> str:
    return f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"


def _fetch_mbtc(cookies: dict[str, str], timeout: float) -> str:
    try:
        response = requests.get(
            _page_url(),
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return ""
        match = re.search(r'"csrf"\s*:\s*"([^"]+)"', html.unescape(response.text))
        return match.group(1) if match else ""
    except requests.RequestException:
        return ""


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"
        f"?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": _page_url(),
    }
    merged = dict(cookies)
    try:
        response = requests.get(url, headers=headers, cookies=merged, timeout=timeout, allow_redirects=False)
        merged.update(_extract_set_cookies(response))
        if response.status_code in (401, 403):
            retry = requests.get(url, headers=headers, cookies=merged, timeout=timeout, allow_redirects=False)
            merged.update(_extract_set_cookies(retry))
    except requests.RequestException:
        pass
    return merged


def _open_session(timeout: float) -> tuple[dict[str, str], str]:
    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError("No hay cookies de QuickSight activas en Firefox.")
    mbtc = _fetch_mbtc(cookies, timeout)
    if not mbtc:
        raise QuickSightAuthError("No se pudo leer el token CSRF de QuickSight.")
    return _warm_up(cookies, mbtc, timeout), mbtc


def is_valid_station_code(value: object) -> bool:
    """Whether a station can safely enter the captured QuickSight filter."""
    return bool(_STATION_CODE_RE.fullmatch(str(value or "").strip().upper()))


def _filter_in(column: str, values: list[str]) -> str:
    return f'((in({{{column}}},[{",".join(f"\"{value}\"" for value in values)}],true)))'


def _build_start_vis(station: str) -> dict[str, Any]:
    station = str(station or "").strip().upper()
    if not is_valid_station_code(station):
        raise QuickSightError("La estación MEP no es válida.")
    fields = []
    for index, column in enumerate(_GROUP_FIELDS):
        last = index == len(_GROUP_FIELDS) - 1
        fields.append({
            "name": column,
            "limit": REQUEST_ROW_LIMIT if last else -1,
            "sort": {"name": _FIELD_STATION, "dir": "ASC"} if last else None,
            "chartContext": column,
            "offset": 0 if last else -1,
            "partitioned": False,
        })
    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": [{"name": "!calc.count.0.0", "func": "CUSTOM"}]},
                "clsSupport": True,
                "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                "filterExpressions": [_filter_in(_FIELD_STATION, [station])],
                "parameters": {},
            },
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": "Europe/Berlin",
            "streamSourceId": STREAM_SOURCE_ID,
            "datasetVersionAlias": "$STABLE",
            "enableComputations": False,
            "computationsOnly": False,
            "sheetId": SHEET_ID,
            "visualId": VISUAL_ID,
            "visualArn": None,
            "visualType": "TABLE",
            "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()),
            "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _category_key(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    aliases = {
        "dsc mep": "dsc", "dcr mep": "dcr", "capacity reliability mep": "capacity_reliability",
        "doosd mep": "doosd", "dvic mep": "dvic", "hv mep": "hv",
        "lor mep": "lor", "pm mep": "pm", "rental debt mep": "rental_debt",
        "armada debt mep": "armada_debt", "safety distraction": "safety_distraction",
        "safety fico": "safety_fico", "safety following distance": "safety_following_distance",
        "safety sign signal violations": "safety_sign_signal_violations",
        "safety speeding": "safety_speeding", "dnr-c mep": "unavailable",
        "netradyne obstruction mep": "unavailable",
    }
    return aliases.get(normalized, "unavailable")


# A DSP that is exiting or temporary surfaces on the dashboard with this MEP
# status; its open cases must not appear in the tool (user instruction: "a DSP
# that should not appear keeps appearing"). Matched on the verified status text
# only, case-insensitively and with whitespace collapsed, so minor spacing or
# casing variants from QuickSight are also excluded — never by DSP code.
_EXIT_TEMPORARY_MEP_STATUSES = frozenset({"dsp exit / temporary dsp"})


def _is_exit_or_temporary(mep_status: Any) -> bool:
    """Whether a MEP status marks an exiting/temporary DSP to drop."""
    return " ".join(str(mep_status or "").strip().lower().split()) in _EXIT_TEMPORARY_MEP_STATUSES


def _configured_dsp_codes() -> frozenset[str]:
    """Configured DSP roster (``ALLOWED_DSPS``), upper-cased.

    Mirrors ``scorecard_exceptions_routes._allowed_dsp_set``: an
    empty/unreadable roster yields an empty set, which ``_dsp_allowed``
    reads as "filter disabled" so an unconfigured station is not zeroed
    out (established house convention).
    """
    import os

    return frozenset(d.upper() for d in parse_allowed_dsps(os.environ.get("ALLOWED_DSPS", "")))


def _dsp_allowed(dsp: Any, allowed: frozenset[str]) -> bool:
    """Whether the row's DSP belongs to the configured roster.

    Mirrors ``scorecard_exceptions_routes._dsp_allowed`` exactly: an empty
    roster disables the filter (station not zeroed out), and a blank DSP
    passes — a row without attribution is still the station's own data,
    not a foreign DSP.
    """
    if not allowed:
        return True
    code = str(dsp or "").strip().upper()
    return not code or code in allowed


def normalize_mep_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize QuickSight groups to the stable frontend row contract."""
    allowed = _configured_dsp_codes()
    rows: list[dict[str, Any]] = []
    for item in items or []:
        group = item.get("group") if isinstance(item, dict) else None
        if not isinstance(group, list):
            continue
        values = [str(group[index] or "").strip() if index < len(group) else "" for index in range(len(_GROUP_FIELDS))]
        _station, dsp, mep_status, mep_name, master_sim, sim_status = values
        if not any(values):
            continue
        if not _dsp_allowed(dsp, allowed):
            continue
        if _is_exit_or_temporary(mep_status):
            continue
        category = _category_key(mep_name)
        rows.append({
            "dsp": dsp,
            "mep_name": mep_name,
            "mep_category": category,
            "mep_status": mep_status,
            "master_sim": master_sim,
            "sim_status": sim_status,
            "is_simtt": bool(master_sim),
            "wiki_url": _CATEGORY_URLS.get(category, WIKI_PORTAL_URL),
            "guidance_available": category in _CATEGORY_URLS,
        })
    return rows


def mep_ore_login_status() -> dict[str, Any]:
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def fetch_mep_rows(station: str, *, timeout: float = 40.0, cookies: dict[str, str] | None = None, mbtc: str | None = None) -> dict[str, Any]:
    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)
    frame = _stream_vis(_build_start_vis(station), cookies, mbtc, timeout)
    if frame is None:
        raise QuickSightError("QuickSight no devolvió la tabla MEP.")
    raw_rows = frame.get("data", [])
    if not isinstance(raw_rows, list):
        raise QuickSightError("QuickSight devolvió filas MEP inválidas.")
    return {
        "rows": normalize_mep_rows(raw_rows),
        # The shared WebSocket primitive receives one table frame only. At
        # its documented cap we cannot prove the station result is complete,
        # so surface a conservative flag to the API/UI instead of claiming
        # full coverage.
        "possibly_truncated": len(raw_rows) >= QUICKSIGHT_TABLE_FRAME_MAX_ROWS,
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


__all__ = [
    "DASHBOARD_ID", "SHEET_ID", "VISUAL_ID", "STREAM_SOURCE_ID", "LOGIN_URL", "MEP_SIMTT_PORTAL_URL", "WIKI_PORTAL_URL", "REQUEST_ROW_LIMIT", "QUICKSIGHT_TABLE_FRAME_MAX_ROWS",
    "QuickSightAuthError", "QuickSightError", "_build_start_vis", "is_valid_station_code", "normalize_mep_rows", "_is_exit_or_temporary", "_dsp_allowed", "_configured_dsp_codes",
    "mep_ore_login_status", "fetch_mep_rows",
]
