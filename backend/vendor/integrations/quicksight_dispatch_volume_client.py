"""QuickSight **Daily Dispatch Planning** integration (Forecast UTR).

Pulls the per-day forecast volumes for future weeks from the "1. Volume
Summary View" pivot of dashboard ``231ee670`` (same QuickSight account and
Firefox/Midway session as the scheduling / forecast / Phoenix clients).
Feeds the Forecast UTR tool (AM):

* ``sideline_out``             -> " 2a. Sideline Out"
* ``dispatch_volume_capped``   -> " 7. Dispatch Volume - Constrained"

All ids below were mapped from the operator's session HAR (2026-08-29) and
verified LIVE the same day (DCZ4: 226 rows, weeks 2026-35..2026-52):

* dashboard             ``231ee670-720b-4702-b373-b5ed2db41bfd``
* sheet                 ``…_a056328f-33a7-4555-bac5-f79b88acef0f``
* pivot-table visual    ``…_60a4f66f-40d4-4d0f-8c9b-6fd0b5b89e0f``
* prepared data source  ``b4e839e4-8f33-4303-8449-088fd0d4f105``
* column prefix         ``eb85c903-936b-4bc1-ab3c-7934b6a09845``
    - ColumnId-17 = metric_name (raw tokens like ``sideline_out``)
    - ColumnId-18 = value
    - ColumnId-12 = OFD week ("2026-36"), ColumnId-13 = OFD date
    - ColumnId-7 / ColumnId-8 = station / node (both filtered to the DS)
"""
from __future__ import annotations

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
    _extract_set_cookies,
    _metric_value,
    _read_cookies,
    _stream_vis,
)

DASHBOARD_ID = "231ee670-720b-4702-b373-b5ed2db41bfd"
SHEET_ID = f"{DASHBOARD_ID}_a056328f-33a7-4555-bac5-f79b88acef0f"
VISUAL_ID = f"{DASHBOARD_ID}_60a4f66f-40d4-4d0f-8c9b-6fd0b5b89e0f"
STREAM_SOURCE_ID = "b4e839e4-8f33-4303-8449-088fd0d4f105"
LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"

_P = "eb85c903-936b-4bc1-ab3c-7934b6a09845"
_C_METRIC = f"{_P}.ColumnId-17"
_C_VALUE = f"{_P}.ColumnId-18"
_C_WEEK = f"{_P}.ColumnId-12"
_C_DATE = f"{_P}.ColumnId-13"
_C_ST7 = f"{_P}.ColumnId-7"
_C_ST8 = f"{_P}.ColumnId-8"

# Raw metric tokens (dataset values of ColumnId-17) -> stable keys we expose.
METRICS: dict[str, str] = {
    "dispatch_volume_capped": "volume",       # 7. Dispatch Volume - Constrained
    "sideline_out": "sideline_out",           # 2a. Sideline Out
}


def _page_url() -> str:
    return LOGIN_URL


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


def _build_start_vis(station: str) -> dict[str, Any]:
    group_ids = [_C_METRIC, _C_WEEK, _C_DATE]
    last = len(group_ids) - 1
    fields = []
    for i, cid in enumerate(group_ids):
        if i == last:
            fields.append({"name": cid, "limit": 50000,
                           "sort": {"name": _C_DATE, "dir": "ASC"},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    metrics = [{"name": _C_VALUE, "func": "SUM", "chartContext": _C_VALUE},
               {"name": "!calc.count.0.0", "func": "CUSTOM"}]
    flist = ",".join(f'"{tok}"' for tok in METRICS)
    filters = [
        f'((in({{{_C_ST7}}},["{station}"],true)))',
        f'((in({{{_C_ST8}}},["{station}"],true)))',
        f'((in({{{_C_METRIC}}},[{flist}],true)))',
    ]
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {"excludeCountStar": True, "group": {"fields": fields, "metrics": metrics},
                    "clsSupport": True,
                    "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                    "filterExpressions": filters, "parameters": {}},
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Madrid",
            "streamSourceId": STREAM_SOURCE_ID, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": SHEET_ID, "visualId": VISUAL_ID, "visualArn": None,
            "visualType": "TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def volume_login_status() -> dict[str, Any]:
    """Whether the Firefox session is live for the volume dashboard."""
    from app.integrations.quicksight_scheduling_client import dashboard_login_status

    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def fetch_volume_forecast(
    *, station: str, timeout: float = 45.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
) -> dict[str, Any]:
    """Per-day forecast rows for ``station``.

    Returns ``{station, weeks, rows}`` where each row is
    ``{metric, week, date, value}`` with ``metric`` in ``volume`` /
    ``sideline_out``, ``week`` like ``2026-36`` and ``date`` ISO.
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para el fetch de volumen.")
    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    data = _stream_vis(_build_start_vis(station), cookies, mbtc, timeout)
    if not data:
        raise QuickSightError("QuickSight no devolvio datos de volumen.")

    rows: list[dict[str, Any]] = []
    for item in data.get("data", []):
        g = item.get("group", [])
        if len(g) < 3:
            continue
        token = str(g[0]).strip()
        key = METRICS.get(token)
        if not key:
            continue
        week = str(g[1]).strip()
        date_iso = str(g[2])[:10]
        mcells = (item.get("current") or {}).get("metrics", {})
        rows.append({
            "metric": key,
            "week": week,
            "date": date_iso,
            "value": _metric_value(mcells.get(_C_VALUE)),
        })

    rows.sort(key=lambda r: (r["date"], r["metric"]))
    weeks = sorted({r["week"] for r in rows})
    return {"station": station, "weeks": weeks, "rows": rows}


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_volume_forecast",
    "volume_login_status",
]
