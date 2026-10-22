"""QuickSight **LMR** integration (Last Mile Rental dashboard).

Pulls the per-DSP vehicle counts by status from the ``LMR Vehicles by
Status - VIN Level (Current Day)`` table of the EU LMR dashboard
(``2e56b2d6``, sheet "Last Mile Rental (LMR)"), reusing the same
Firefox/Midway QuickSight session machinery as the Phoenix/scheduling
clients. Feeds the LMRs of the Fleet Real popup in the Forecast +3Wks
tool (MetisPlan).

Discovery notes (verified live 2026-08-20 against DCT7/CDOR):

* The dashboard's HTTP calls in a HAR are only filter cardinalities —
  the table rows stream over the WebSocket (same lesson as Phoenix).
* Source visual ``1f7a4ca0`` on sheet ``f1816f13``; prepared data source
  ``3880de8d`` = streamSourceId; dataset ``fbf5ada0``.
* ``date_type = "latest"`` is the "Current Day" cut: exactly one row per
  VIN with its live status (without it every historic status row per VIN
  comes back). ``current_vehicle_status`` values: AVAILABLE / HOLD /
  MAINTENANCE\\/REPAIR / ON-RENT / RESERVED / TRANSPORT.
* Filtering by ``reservation_dsp_code`` keeps the DSP attribution also
  for MAINTENANCE/REPAIR and RESERVED vehicles.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import re
import uuid
from typing import Any

import requests

from app.integrations.quicksight_scheduling_client import (
    ALIAS,
    HOST,
    QuickSightAuthError,
    QuickSightError,
    _UA,
    _connect_ws,
    _extract_set_cookies,
    _read_cookies,
    dashboard_login_status,
)

DASHBOARD_ID = "2e56b2d6-380d-4b0d-af10-d8fa47d65fe6"
SHEET_ID = f"{DASHBOARD_ID}_f1816f13-2a68-426b-b81d-433a17c178f0"
VISUAL_ID = f"{DASHBOARD_ID}_1f7a4ca0-648d-4b97-a083-3b4d45e04aff"
STREAM_SOURCE_ID = "3880de8d-5b66-4800-9bd2-32edb8295fc9"
LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"

_P = "fbf5ada0-6aa3-4f41-8359-8b3e140d983e"
_C_DSP = f"{_P}.reservation_dsp_code"
_C_STATUS = f"{_P}.current_vehicle_status"
_C_VIN = f"{_P}.vin"
_C_DATE_TYPE = f"{_P}.date_type"

_GROUP = [_C_DSP, _C_STATUS, _C_VIN]

# Dashboard status literal -> stable key the Fleet Real popup consumes.
_STATUS_KEYS = {
    "ON-RENT": "rent",
    "MAINTENANCE/REPAIR": "maintenance",
    "RESERVED": "reserved",
}


def _page_url() -> str:
    return f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    try:
        resp = requests.get(
            _page_url(),
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies, timeout=timeout, allow_redirects=False,
        )
        if resp.status_code != 200:
            return ""
        m = re.search(r'"csrf"\s*:\s*"([^"]+)"', _html.unescape(resp.text))
        return m.group(1) if m else ""
    except requests.RequestException:
        return ""


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"
        f"?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA, "Accept": "*/*", "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc, "x-amzn-target": "api", "Referer": _page_url(),
    }
    merged = dict(cookies)
    try:
        resp = requests.get(url, headers=headers, cookies=merged, timeout=timeout, allow_redirects=False)
        merged.update(_extract_set_cookies(resp))
        if resp.status_code in (401, 403):
            resp2 = requests.get(url, headers=headers, cookies=merged, timeout=timeout, allow_redirects=False)
            merged.update(_extract_set_cookies(resp2))
    except requests.RequestException:
        pass
    return merged


def _open_session(timeout: float = 35.0) -> tuple[dict[str, str], str]:
    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{HOST}/ en Firefox."
        )
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    if not mbtc:
        raise QuickSightAuthError(
            "No se pudo leer el token CSRF de QuickSight (sesion expirada)."
        )
    cookies = _warm_up(cookies, mbtc, timeout)
    return cookies, mbtc


def _build_start_vis(dsps: list[str]) -> dict[str, Any]:
    last = len(_GROUP) - 1
    fields = []
    for i, cid in enumerate(_GROUP):
        if i == last:
            fields.append({"name": cid, "limit": 50000,
                           "sort": {"name": cid, "dir": "ASC"},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    quoted = ",".join(f'"{d}"' for d in dsps)
    filters = [
        f"((in({{{_C_DSP}}},[{quoted}],true)))",
        f'((in({{{_C_DATE_TYPE}}},["latest"],true)))',
    ]
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {"excludeCountStar": True,
                    "group": {"fields": fields,
                              "metrics": [{"name": "!calc.count.0.0", "func": "CUSTOM"}]},
                    "clsSupport": True,
                    "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                    "filterExpressions": filters, "parameters": {}},
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": STREAM_SOURCE_ID, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": SHEET_ID, "visualId": VISUAL_ID, "visualArn": None,
            "visualType": "TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _stream_vis_or_empty(
    start_vis: dict[str, Any], cookies: dict[str, str], mbtc: str, timeout: float
) -> dict[str, Any] | None:
    """Like the shared ``_stream_vis`` but short-circuits ``NO_DATA_FOUND``.

    DSPs without a single LMR vehicle are a normal outcome here (the
    counts come back as zeros); the shared helper would sit on the socket
    until the timeout because no ``data`` frame ever arrives.
    """
    import json as _json
    import time as _time

    ws, _ = _connect_ws(cookies, mbtc, timeout)
    cid = start_vis["cid"]
    data_msg: dict[str, Any] | None = None
    try:
        ws.send(_json.dumps(start_vis))
        ws.settimeout(timeout)
        deadline = _time.time() + timeout + 5
        while _time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            if "error" in msg:
                raise QuickSightError(
                    f"QuickSight error: {msg.get('error')} - {msg.get('internalMessage')}"
                )
            if msg.get("status") == "NO_DATA_FOUND":
                break
            if "data" in msg and "available" in msg:
                data_msg = msg
                break
    finally:
        try:
            ws.send(_json.dumps({"type": "STOP_VIS", "cids": [cid]}))
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass
    return data_msg


def aggregate_lmr_rows(items: list[dict[str, Any]], dsps: list[str]) -> dict[str, dict[str, int]]:
    """Distinct-VIN counts per DSP and status key.

    ``items`` are the raw ``data`` entries of the stream frame (group =
    [dsp, status, vin]). Every requested DSP is present in the output
    (zeros when the dashboard has no vehicles for it).
    """
    seen: dict[str, set[tuple[str, str]]] = {}
    out: dict[str, dict[str, int]] = {
        dsp: {key: 0 for key in _STATUS_KEYS.values()} for dsp in dsps
    }
    for item in items or []:
        g = item.get("group", [])
        if len(g) < 3:
            continue
        dsp = str(g[0] or "").strip().upper()
        status = str(g[1] or "").strip().upper()
        vin = str(g[2] or "").strip()
        key = _STATUS_KEYS.get(status)
        if not key or dsp not in out or not vin:
            continue
        bucket = seen.setdefault(dsp, set())
        if (key, vin) in bucket:
            continue
        bucket.add((key, vin))
        out[dsp][key] += 1
    return out


def lmr_login_status() -> dict[str, Any]:
    """Whether the Firefox session is live for the LMR dashboard."""
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def fetch_lmr_status(
    dsps: list[str], *, timeout: float = 40.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
) -> dict[str, Any]:
    """Current-day LMR vehicle counts per DSP.

    Returns ``{"dsps": {dsp: {rent, maintenance, reserved}},
    "fetched_at": iso}``. ``rent`` = ON-RENT distinct VINs (the LMRs the
    DSP is actually running); ``maintenance``/``reserved`` are surfaced
    for the hover tooltip but are NOT part of the LMR count.
    """
    codes = sorted({str(d or "").strip().upper() for d in dsps if str(d or "").strip()})
    if not codes:
        raise QuickSightError("Faltan las DSPs para el fetch de LMR.")

    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    data = _stream_vis_or_empty(_build_start_vis(codes), cookies, mbtc, timeout)
    items = (data or {}).get("data", [])
    return {
        "dsps": aggregate_lmr_rows(items, codes),
        "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "aggregate_lmr_rows",
    "fetch_lmr_status",
    "lmr_login_status",
]
