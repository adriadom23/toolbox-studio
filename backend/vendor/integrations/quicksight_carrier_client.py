"""QuickSight integration for the **Pre-DNR (Carrier Investigation)** table.

This is the carrier-investigation sibling of ``quicksight_client`` (which
serves the DPR dashboard). It pulls the per-DSP *Investigations vs
Responses* table straight from the Carrier Investigation dashboard,
reusing the same Midway/QuickSight Firefox session cookies and the same
WebSocket ``START_VIS`` mechanism already proven for DPR — so the operator
no longer needs to export a CSV from QuickSight and drag it into the tool.

All ids below were mapped from a captured session HAR of the dashboard
(``us-east-1.quicksight.aws.amazon.com``):

* dashboard            ``303366a2-c175-47c1-99fc-76ffa9cd83d1``
* sheet "Process …"    ``…_40ecb3e8-fa01-42bf-9249-37f06d8e88d9``
* table visual         ``…_0626f91c-913f-498d-bc78-14c708238427``
  ("Carrier Investigation Metric Summary", rows = delivery_station +
  dsp_shortcode, measures = Investigations Raised + DSP Responses)
* prepared data source ``1c2d56ad-616c-45a2-9320-5297cc223435``
* table column prefix  ``7096ffb5-bdb4-4058-b4c6-9dde27bb623a``
* measure: investigations ``bbae191d-e6a3-424c-94a2-fce9010f9815`` (CUSTOM)
* measure: responses      ``0b4dceab-c535-46b2-afbd-7690e54c0008`` (CUSTOM)

Every id is overridable via environment variables so a future dashboard
revision can be re-pointed without a rebuild.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import requests

# Reuse the proven session/cookie/handshake plumbing from the DPR client.
from app.integrations.quicksight_client import (
    QUICKSIGHT_ALIAS,
    QUICKSIGHT_HOST,
    QuickSightAuthError,
    QuickSightError,
    _extract_set_cookies,
    _filter_list,
    _is_session_for_alias,
    _parse_handshake_cookies,
    _read_cookies,
    websocket,
)


# ---------------------------------------------------------------------------
# Dashboard config (mapped from the HAR; overridable via env).
# ---------------------------------------------------------------------------

DASHBOARD_ID = (
    os.environ.get("CARRIER_QS_DASHBOARD_ID", "").strip()
    or "303366a2-c175-47c1-99fc-76ffa9cd83d1"
)
SHEET_ID = (
    os.environ.get("CARRIER_QS_SHEET_ID", "").strip()
    or "303366a2-c175-47c1-99fc-76ffa9cd83d1_40ecb3e8-fa01-42bf-9249-37f06d8e88d9"
)
VISUAL_ID = (
    os.environ.get("CARRIER_QS_VISUAL_ID", "").strip()
    or "303366a2-c175-47c1-99fc-76ffa9cd83d1_0626f91c-913f-498d-bc78-14c708238427"
)
DEFAULT_STREAM_SOURCE_ID = (
    os.environ.get("CARRIER_QS_STREAM_SOURCE_ID", "").strip()
    or "1c2d56ad-616c-45a2-9320-5297cc223435"
)

# Prefix shared by every physical table column on this prepared data source.
_COL_PREFIX = "7096ffb5-bdb4-4058-b4c6-9dde27bb623a"
_DELIVERY_STATION_ID = f"{_COL_PREFIX}.delivery_station"
_DSP_ID = f"{_COL_PREFIX}.dsp_shortcode"
_COUNTRY_ID = f"{_COL_PREFIX}.country"
_WEEK_ID = f"{_COL_PREFIX}.investigation_week"

# Ordered group (dimensions). The last field is the one QuickSight paginates.
# ``investigation_week`` lets us scope to the current (latest) week — the
# dashboard labels weeks as ``YYYY-WW`` (ISO, zero-padded, lexically sortable).
_GROUP: list[tuple[str, str]] = [
    (_DELIVERY_STATION_ID, "delivery_station"),
    (_DSP_ID, "dsp_shortcode"),
    (_WEEK_ID, "investigation_week"),
]
# Measures, computed from PHYSICAL columns (no analysis calc fields are
# exposed for this dashboard). QuickSight ``COUNT`` counts NON-NULL values
# (SQL semantics), so:
#   * investigations = COUNT(tracking_id)        -> every investigation row
#   * responses      = COUNT(dsp_response_time)  -> rows the DSP answered
# Override the column/func via env if the dataset ever changes.
_METRICS: list[dict[str, str]] = [
    {
        "out": "investigations",
        "name": os.environ.get("CARRIER_QS_INVESTIGATIONS_COL", "").strip()
        or f"{_COL_PREFIX}.tracking_id",
        "func": os.environ.get("CARRIER_QS_INVESTIGATIONS_FUNC", "").strip() or "COUNT",
    },
    {
        "out": "responses",
        "name": os.environ.get("CARRIER_QS_RESPONSES_COL", "").strip()
        or f"{_COL_PREFIX}.dsp_response_time",
        "func": os.environ.get("CARRIER_QS_RESPONSES_FUNC", "").strip() or "COUNT",
    },
]

_DEFAULT_MBTC = "b6e3n16w1F1SM%2Fd6%2FwtLU7mtC5IHnwPAcAtBpBchRgc%3D"

CARRIER_LOGIN_URL = (
    f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}"
)

_LIMIT = 10000

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"


def _cell_value(cell: Any) -> float | None:
    """Read a metric cell regardless of aggregation key (count/sum/custom)."""
    if not isinstance(cell, dict) or not cell:
        return None
    for key in ("count", "sum", "custom", "min", "max", "avg", "average"):
        if key in cell:
            val = cell[key]
            if isinstance(val, dict):
                val = val.get("parsedValue", val.get("source"))
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


def _mbtc() -> str:
    return os.environ.get("QUICKSIGHT_MBTC", "").strip() or _DEFAULT_MBTC


def _stream_source_id() -> str:
    return os.environ.get("CARRIER_QS_STREAM_SOURCE_ID", "").strip() or DEFAULT_STREAM_SOURCE_ID


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Read the live per-session csrf token from the dashboard HTML page.

    The hardcoded default rotates and gets rejected with "csrf token missing
    or not match", so we must read the live one (same trick as the DPR client).
    """
    import html as _html
    import re

    url = f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies, timeout=timeout, allow_redirects=False,
        )
        if resp.status_code != 200:
            return _mbtc()
        decoded = _html.unescape(resp.text)
        m = re.search(r'"csrf"\s*:\s*"([^"]+)"', decoded)
        if m:
            return m.group(1)
    except requests.RequestException:
        pass
    return _mbtc()


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    """Bootstrap the session against the carrier dashboard.

    QuickSight often answers the first API call with a 403 but sets the
    ``aws-ds-access-token`` cookie; a second call with that cookie completes
    the bootstrap so the WebSocket handshake later succeeds. Mirrors the DPR
    client's ``_warm_up``.
    """
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA, "Accept": "*/*", "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc, "x-amzn-target": "api",
        "Referer": f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}",
    }
    merged = dict(cookies)
    try:
        resp = requests.get(url, headers=headers, cookies=merged, timeout=timeout, allow_redirects=False)
        merged.update(_extract_set_cookies(resp))
        if resp.status_code in (403, 401):
            resp2 = requests.get(url, headers=headers, cookies=merged, timeout=timeout, allow_redirects=False)
            merged.update(_extract_set_cookies(resp2))
    except requests.RequestException:
        pass
    return merged


def carrier_login_status() -> dict[str, Any]:
    """Check whether the QuickSight session can read the carrier dashboard."""
    cookies = _read_cookies()
    if not cookies:
        return {
            "ready": False, "cookie_count": 0, "correct_account": False,
            "host": QUICKSIGHT_HOST, "reason": "no_cookies",
        }
    correct_alias = _is_session_for_alias(cookies)
    mbtc = _fetch_mbtc(cookies)
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=1"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA, "Accept": "*/*", "x-amzn-csrf-token": mbtc, "x-amzn-target": "api",
        "Referer": f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}",
    }
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=12, allow_redirects=False)
        body = resp.text.lower()
        live = resp.status_code == 200 and "authenticate" not in body and "error" not in body[:50]
        return {
            "ready": live, "cookie_count": len(cookies), "correct_account": correct_alias,
            "host": QUICKSIGHT_HOST,
            "reason": "" if live else ("wrong_account" if not correct_alias else "session_expired"),
        }
    except requests.RequestException:
        return {
            "ready": False, "cookie_count": len(cookies), "correct_account": correct_alias,
            "host": QUICKSIGHT_HOST, "reason": "network_error",
        }


def _build_start_vis(
    stations: list[str], country: str | None, weeks: list[str] | None
) -> dict[str, Any]:
    last = len(_GROUP) - 1
    fields = []
    for i, (cid, _name) in enumerate(_GROUP):
        if i == last:
            fields.append({
                "name": cid, "limit": _LIMIT,
                "sort": {"name": _DELIVERY_STATION_ID, "dir": "ASC"},
                "chartContext": cid, "offset": 0, "partitioned": False,
            })
        else:
            fields.append({
                "name": cid, "limit": -1, "sort": None,
                "chartContext": cid, "offset": -1, "partitioned": False,
            })
    # Measures computed from physical columns (COUNT = non-null count).
    metrics = [{"name": m["name"], "func": m["func"], "chartContext": m["name"]} for m in _METRICS]

    filters: list[str] = []
    if stations:
        filters.append(f'((in({{{_DELIVERY_STATION_ID}}},[{_filter_list(stations)}],true)))')
    if country:
        filters.append(f'((in({{{_COUNTRY_ID}}},[{_filter_list([country])}],true)))')
    if weeks:
        filters.append(f'((in({{{_WEEK_ID}}},[{_filter_list(weeks)}],true)))')

    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metrics},
                "clsSupport": True,
                "calculatedFields": [],
                "filterExpressions": filters,
                "parameters": {},
            },
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": "Europe/Berlin",
            "streamSourceId": _stream_source_id(),
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


def fetch_carrier_rows(
    *,
    stations: list[str] | None = None,
    country: str | None = None,
    weeks: list[str] | None = None,
    timeout: float = 25.0,
    debug: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pull the per-(station, DSP) carrier investigation table from QuickSight.

    Returns a list of ``{"delivery_station", "dsp_shortcode",
    "investigations", "responses"}`` dicts. Raises :class:`QuickSightAuthError`
    when the Firefox session is missing/expired and :class:`QuickSightError`
    when the dashboard returns no data.

    When ``debug`` is a dict, the raw ``START_VIS`` payload and every frame
    received are recorded into it for diagnostics.
    """
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")

    stations = [s.strip().upper() for s in (stations or []) if s and s.strip()]
    weeks = [str(w).strip() for w in (weeks or []) if str(w or "").strip()]
    country = (country or "").strip().upper() or None

    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{QUICKSIGHT_HOST}/ en Firefox."
        )
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    cookies = _warm_up(cookies, mbtc, timeout)

    ws_url = f"wss://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/websocket/?mbtc={mbtc}"

    ws = None
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            ws = websocket.create_connection(
                ws_url,
                header=[f"User-Agent: {_UA}", "Origin: https://" + QUICKSIGHT_HOST],
                cookie=cookie_header, timeout=timeout, suppress_origin=True,
            )
            break
        except Exception as exc:  # noqa: BLE001
            exc_str = str(exc)
            if "(re)authenticate" in exc_str or "return to https://console" in exc_str:
                raise QuickSightAuthError(
                    "Sesion de AWS expirada. Cierra la pestana de QuickSight en Firefox, "
                    "vuelve a abrirla para renovar la sesion, y reintenta."
                )
            if attempt == 0 and "403" in exc_str:
                new_cookies = _parse_handshake_cookies(exc)
                if new_cookies:
                    cookies = {**cookies, **new_cookies}
                    continue
            if attempt == 0 and ("302" in exc_str or "url is invalid" in exc_str.lower()):
                raise QuickSightAuthError(
                    "Sesion de QuickSight no activa en la cuenta correcta. "
                    "Abre el dashboard en Firefox e inicia sesion."
                )
            raise QuickSightAuthError(
                f"No se pudo conectar al WebSocket de QuickSight: {exc_str[:200]}"
            )

    if ws is None:
        raise QuickSightAuthError("No se pudo conectar al WebSocket de QuickSight.")

    start_vis = _build_start_vis(stations, country, weeks)
    cid = start_vis["cid"]
    if debug is not None:
        debug["start_vis"] = start_vis
        debug["frames"] = []
    data_msg = None
    # Track whether the server acknowledged the query at all. A WebSocket that
    # connects and receives control frames for our ``cid`` (status/type/cid
    # frames precede the terminal ``data``+``available`` frame) but no data
    # rows is a *successful empty* extraction — e.g. a week filter matching
    # zero rows. We only treat a complete silence (no server frame whatsoever)
    # as a genuine transport failure.
    saw_server_ack = False
    try:
        ws.send(json.dumps(start_vis))
        ws.settimeout(timeout)
        deadline = time.time() + timeout + 5
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if debug is not None and len(debug["frames"]) < 40:
                debug["frames"].append(msg)
            if isinstance(msg, dict) and msg:
                # Any well-formed frame from the server is an acknowledgement
                # that the START_VIS was accepted and processed.
                saw_server_ack = True
            if "error" in msg:
                raise QuickSightError(f"QuickSight error: {msg.get('error')} - {msg.get('details')}")
            if "data" in msg and "available" in msg:
                data_msg = msg
                break
    finally:
        try:
            ws.send(json.dumps({"type": "STOP_VIS", "cids": [cid]}))
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass

    if debug is not None:
        debug["saw_server_ack"] = saw_server_ack

    if not data_msg:
        # The server acknowledged the query but returned no terminal data
        # frame -> no rows for this filter (valid empty result). Only raise
        # when nothing was received at all (true transport breakage).
        if saw_server_ack:
            return []
        raise QuickSightError("QuickSight no devolvio datos de Carrier Investigation.")

    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        g = item.get("group", [])
        mcells = (item.get("current") or {}).get("metrics", {})
        record: dict[str, Any] = {}
        for idx, (_cid, name) in enumerate(_GROUP):
            record[name] = g[idx] if idx < len(g) else None
        for m in _METRICS:
            val = _cell_value(mcells.get(m["name"]))
            record[m["out"]] = int(val) if val is not None else 0
        rows.append(record)
    return rows


def latest_week(rows: list[dict[str, Any]]) -> str:
    """Return the most recent ``investigation_week`` present in ``rows``."""
    weeks = [str(r.get("investigation_week") or "") for r in rows if r.get("investigation_week")]
    return max(weeks) if weeks else ""


def fetch_carrier_rows_latest(
    *,
    stations: list[str] | None = None,
    country: str | None = None,
    timeout: float = 25.0,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch ALL weeks and scope locally to the latest populated week.

    Centralizes the "latest available week" semantics (what the dashboard
    shows by default) so the route and any future caller share it. Returns
    ``(rows_for_latest_week, latest_week)``; both empty when the dataset has
    no rows for these stations.
    """
    rows = fetch_carrier_rows(stations=stations, country=country, weeks=None, timeout=timeout)
    latest = latest_week(rows)
    if latest:
        rows = [r for r in rows if str(r.get("investigation_week") or "") == latest]
    return rows, latest


__all__ = [
    "DASHBOARD_ID",
    "CARRIER_LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "carrier_login_status",
    "fetch_carrier_rows",
    "fetch_carrier_rows_latest",
    "latest_week",
]
