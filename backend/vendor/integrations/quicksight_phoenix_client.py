"""QuickSight **Phoenix** integration (EU Fleet Planning dashboard).

Pulls the weekly van/fleet capacity per DSP from the ``Fleet Plan -
Summary`` sheet of dashboard ``52ffc7c8`` (same QuickSight account and
Firefox/Midway session as the scheduling & forecast clients). Feeds the
"Phoenix" tab of the Forecast +3Wks tool (MetisPlan).

Source visual (verified live 2026-08-17 against DCZ4):

* ``DS-DSP Aggregate: Weekly Fleet Planning Summary - Country > DSP >
  Station drill down view`` (visual ``6165beb5``) — one row per
  DSP x week (``week_start``), weekly granularity only (the dataset has
  no daily column; a van capacity applies to every day of its week).

Metric mapping (dataset ``25fc8a87``):

* ``demand``                                  -> Okami demand (OTR POR)
* ``total_capacity_calc``                     -> Total Fleet Capacity
* ``total_operable_capacity_vs_noncon_vor``   -> Total Operable vs Non Con VOR
* ``total_operable_capacity_vs_total_vor``    -> Total Operable vs Total VOR

Plus the Fleet Capacity breakdown the dashboard shows on hover
(armadas / rentals / self sourced / LMR / EV armadas / migrated SeSo /
warranty + other + total VOR).
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
    _extract_set_cookies,
    _metric_value,
    _read_cookies,
    _stream_vis,
)

DASHBOARD_ID = "52ffc7c8-c0c2-40b4-9fac-7ca252cc3177"
SHEET_ID = f"{DASHBOARD_ID}_65857a61-8aeb-45ca-bff3-9e62dbb915fc"
VISUAL_ID = f"{DASHBOARD_ID}_6165beb5-f278-4b64-b578-172326005437"
STREAM_SOURCE_ID = "7c2415e4-0ce6-4b2f-9ee0-eb3f6420761e"
LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"

_P = "25fc8a87-7780-46ac-a24a-9250c30a4531"
_C_COUNTRY = f"{_P}.country"
_C_STATION = f"{_P}.station"
_C_DSP = f"{_P}.dsp"
_C_WEEK = f"{_P}.demand_year_week"
_C_WEEK_START = f"{_P}.week_start"

_GROUP = [_C_COUNTRY, _C_STATION, _C_DSP, _C_WEEK, _C_WEEK_START]

# Headline metrics (the four columns the Phoenix tab tables show).
_MAIN_METRICS: dict[str, str] = {
    f"{_P}.demand": "demand",
    f"{_P}.total_capacity_calc": "total_capacity",
    f"{_P}.total_operable_capacity_vs_noncon_vor": "operable_vs_noncon_vor",
    f"{_P}.total_operable_capacity_vs_total_vor": "operable_vs_total_vor",
}

# Fleet Capacity breakdown (dashboard hover tooltip). Keys are stable ids
# the frontend maps to display labels; only non-zero entries are shown.
_BREAKDOWN_METRICS: dict[str, str] = {
    f"{_P}.armada_total_calc": "armadas",
    f"{_P}.rentals_total_calc": "rentals",
    f"{_P}.seso_total_calc": "self_sourced",
    f"{_P}.lmr_total_calc": "lmr",
    f"{_P}.ev_total_armadas": "ev_armadas",
    f"{_P}.seso_migrated_total_calc": "migrated_seso",
    f"{_P}.warranty_vor": "warranty_vor",
    f"{_P}.other_vor": "other_vor",
    f"{_P}.total_vor": "total_vor",
}

# Rolling window mirroring the dashboard subtitle ("Rolling 15 week —
# WK-2 through WK+13").
_WEEKS_BACK = 2
_WEEKS_AHEAD = 13


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


def _build_start_vis(station: str, country: str | None) -> dict[str, Any]:
    metric_ids = list(_MAIN_METRICS) + list(_BREAKDOWN_METRICS)
    last = len(_GROUP) - 1
    fields = []
    for i, cid in enumerate(_GROUP):
        if i == last:
            fields.append({"name": cid, "limit": 50000,
                           "sort": {"name": _C_WEEK_START, "dir": "ASC"},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    metrics = [{"name": cid, "func": "SUM", "chartContext": cid} for cid in metric_ids]
    metrics.append({"name": "!calc.count.0.0", "func": "CUSTOM"})
    # Country first, then station — same drill the dashboard applies.
    filters = []
    if country:
        filters.append(f'((in({{{_C_COUNTRY}}},["{country}"],true)))')
    filters.append(f'((in({{{_C_STATION}}},["{station}"],true)))')
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {"excludeCountStar": True, "group": {"fields": fields, "metrics": metrics},
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


def phoenix_login_status() -> dict[str, Any]:
    """Whether the Firefox session is live for the *Phoenix* dashboard."""
    from app.integrations.quicksight_scheduling_client import dashboard_login_status

    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def _week_window(today: _dt.date | None = None) -> tuple[_dt.date, _dt.date]:
    """[monday(WK-_WEEKS_BACK), monday(WK+_WEEKS_AHEAD)] week_start bounds.

    The dataset's ``week_start`` is a Sunday; anchor on the current week's
    Sunday so the current week always survives the cut.
    """
    today = today or _dt.date.today()
    # Sunday-anchored week start (QuickSight weekStart=SUNDAY).
    week_start = today - _dt.timedelta(days=(today.weekday() + 1) % 7)
    lo = week_start - _dt.timedelta(weeks=_WEEKS_BACK)
    hi = week_start + _dt.timedelta(weeks=_WEEKS_AHEAD)
    return lo, hi


def fetch_fleet_capacity(
    *, station: str, country: str | None = None, timeout: float = 40.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
) -> dict[str, Any]:
    """Weekly fleet/van capacity rows per DSP for ``station``.

    Returns ``{station, country, weeks: [...], dsps: [...], rows: [...]}``
    where each row is one DSP x week with the four headline metrics and
    the Fleet Capacity breakdown. Weeks are trimmed to the dashboard's
    rolling window (WK-2 .. WK+13).
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para el fetch de Phoenix.")
    country = (country or "").strip().upper() or None

    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    data = _stream_vis(_build_start_vis(station, country), cookies, mbtc, timeout)
    if not data:
        raise QuickSightError("QuickSight no devolvio datos de Phoenix.")

    lo, hi = _week_window()
    rows: list[dict[str, Any]] = []
    metric_ids = list(_MAIN_METRICS) + list(_BREAKDOWN_METRICS)
    for item in data.get("data", []):
        g = item.get("group", [])
        if len(g) < 5:
            continue
        row_country = str(g[0]).strip().upper()
        row_station = str(g[1]).strip().upper()
        dsp = str(g[2]).strip()
        week = str(g[3]).strip()
        week_start_raw = str(g[4])[:10]
        if row_station != station or not dsp:
            continue
        try:
            week_start = _dt.date.fromisoformat(week_start_raw)
        except ValueError:
            continue
        if week_start < lo or week_start > hi:
            continue
        mcells = (item.get("current") or {}).get("metrics", {})
        values: dict[str, float | None] = {}
        for mid in metric_ids:
            key = _MAIN_METRICS.get(mid) or _BREAKDOWN_METRICS[mid]
            values[key] = _metric_value(mcells.get(mid))
        rows.append({
            "country": row_country,
            "dsp": dsp,
            "week": week,
            "week_start": week_start_raw,
            "demand": values.get("demand"),
            "total_capacity": values.get("total_capacity"),
            "operable_vs_noncon_vor": values.get("operable_vs_noncon_vor"),
            "operable_vs_total_vor": values.get("operable_vs_total_vor"),
            "breakdown": {k: values.get(k) for k in _BREAKDOWN_METRICS.values()},
        })

    rows.sort(key=lambda r: (r["dsp"], r["week_start"]))
    weeks = sorted({r["week"] for r in rows}, key=lambda w: min(
        r["week_start"] for r in rows if r["week"] == w
    ))
    dsps = sorted({r["dsp"] for r in rows})
    return {
        "station": station,
        "country": country or (rows[0]["country"] if rows else ""),
        "weeks": weeks,
        "dsps": dsps,
        "rows": rows,
    }


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_fleet_capacity",
    "phoenix_login_status",
]
