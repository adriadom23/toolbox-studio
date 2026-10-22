"""QuickSight **Station Inbound Curves** integration (Sweepers, Handover AM).

Pulls two visuals of the ``Truck Schedule Monitor (D-1)`` sheet of the AMZL
BAT *Station Inbound Curves* dashboard (``ef16dc15-...``), reusing the same
Firefox/Midway QuickSight session machinery as the scheduling/cube clients:

* **Forecast Curves** (LINE_CHART) — cumulative inbound/induct forecast
  curves over the arrival timestamp. The frontend redraws them.
* **VRID Details** (PIVOT_TABLE) — one row per truck (VRID) with its
  planned arrival, origin/destination, equipment type and the
  ``Forecast (CONST)`` volume (physical column ``capped_volume``).

Both visuals are filtered server-side by ``dest_site`` (station) and
``ofd_date_str`` (the D+1 plan date the Handover cares about).

The dashboard layout (sheet/visual/stream/dataset ids) was mapped with
``backend/scripts/qs_discover.py`` from the operator's HAR capture. The
calculated fields resolve to physical columns (verified in the embedded
dashboard model), so every query below uses physical columns only.
"""
from __future__ import annotations

import html as _html
import re
import uuid
from datetime import date as _date
from typing import Any

import requests

from app.integrations.quicksight_scheduling_client import (
    ALIAS,
    HOST,
    QuickSightAuthError,
    QuickSightError,
    _extract_set_cookies,
    _read_cookies,
    _stream_vis,
    dashboard_login_status,
)


def _metric_value(cell: Any) -> float | None:
    """Decode a metric cell regardless of its aggregation key.

    The shared ``_metric_value`` only reads ``sum``/``custom``; the curves
    query uses MAX so its cells come back as ``{"max": <value>}``.
    """
    if not isinstance(cell, dict) or not cell:
        return None
    val = None
    for key in ("sum", "max", "min", "avg", "custom"):
        if key in cell:
            val = cell[key]
            break
    if isinstance(val, dict):
        val = val.get("parsedValue", val.get("source"))
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

import os

# ── Dashboard map (HAR + qs_discover), overridable via env ──────────────────
DASHBOARD_ID = os.getenv("QS_SWEEPERS_DASHBOARD_ID", "").strip() or "ef16dc15-25d9-4610-b7a1-240808dc60a4"
SHEET_ID = (
    os.getenv("QS_SWEEPERS_SHEET_ID", "").strip()
    or f"{DASHBOARD_ID}_fcc4b7e6-8878-439b-bdc3-a04c03947adb"  # Truck Schedule Monitor (D-1)
)
CURVES_VISUAL_ID = (
    os.getenv("QS_SWEEPERS_CURVES_VISUAL_ID", "").strip()
    or f"{DASHBOARD_ID}_554d8935-157e-4e34-966c-c5d82862b897"  # Forecast Curves
)
CURVES_STREAM_ID = os.getenv("QS_SWEEPERS_CURVES_STREAM_ID", "").strip() or "7d069246-12b8-4f04-9730-4fddbb3f020a"
TRUCKS_VISUAL_ID = (
    os.getenv("QS_SWEEPERS_TRUCKS_VISUAL_ID", "").strip()
    or f"{DASHBOARD_ID}_3ed80d0a-082e-454d-8115-47db1f13f98e"  # VRID Details
)
TRUCKS_STREAM_ID = os.getenv("QS_SWEEPERS_TRUCKS_STREAM_ID", "").strip() or "192a7ec2-6114-4a2e-be72-3c1a899984d1"

# Dataset namespaces (physical columns).
_DS_CURVES = "40b8e0b3-695f-46ff-9665-740677e777fd"
_DS_TRUCKS = "07caf101-c1e7-4d46-8582-2964fab54af5"

LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"

# Curve series: label -> physical cumulative column (MAX per timestamp, the
# same aggregation the dashboard's calculated Y-axes use).
CURVE_SERIES: list[tuple[str, str]] = [
    ("Inbound Forecast", f"{_DS_CURVES}.volume_inbound_forecast_cummulative_final"),
    ("Induct Forecast (D0)", f"{_DS_CURVES}.volume_induct_forecast_cumulative_d1"),
    ("Inbound Actual", f"{_DS_CURVES}.volume_inbound_actual_cumulative"),
    ("Capacity", f"{_DS_CURVES}.cumulative_hourly_capacity"),
]

# Truck table dimensions (physical) in display order.
TRUCK_DIMS: list[tuple[str, str]] = [
    ("arrival", f"{_DS_TRUCKS}.arrival_time_dest_local"),
    ("origin", f"{_DS_TRUCKS}.orig_site"),
    ("destination", f"{_DS_TRUCKS}.dest_site"),
    ("vrid", f"{_DS_TRUCKS}.vrid"),
    ("equipment_type", f"{_DS_TRUCKS}.equipment_type"),
]
TRUCK_METRIC = f"{_DS_TRUCKS}.capped_volume"  # = "Forecast (CONST)"


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
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=175"
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


def sweepers_login_status() -> dict[str, Any]:
    """Whether the Firefox session can read the Inbound Curves dashboard."""
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def _open_session(timeout: float = 30.0) -> tuple[dict[str, str], str]:
    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{HOST}/ en Firefox."
        )
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    if not mbtc:
        raise QuickSightAuthError(
            "No se pudo leer el token CSRF. La sesion de QuickSight probablemente expiro."
        )
    cookies = _warm_up(cookies, mbtc, timeout)
    return cookies, mbtc


def _filter_in(col_id: str, values: list[str]) -> str:
    quoted = ",".join(f'"{v}"' for v in values)
    return f"((in({{{col_id}}},[{quoted}],true)))"


def _start_vis(
    *,
    visual_id: str,
    visual_type: str,
    stream_id: str,
    dims: list[str],
    metrics: list[tuple[str, str]],
    filters: list[str],
    limit: int = 5000,
) -> dict[str, Any]:
    """Build a START_VIS frame (same shape as the scheduling client)."""
    last = len(dims) - 1
    fields = []
    for i, col in enumerate(dims):
        if i == last:
            fields.append({
                "name": col, "limit": limit,
                "sort": {"name": dims[0], "dir": "ASC"},
                "chartContext": col, "offset": 0, "partitioned": False,
            })
        else:
            fields.append({
                "name": col, "limit": -1, "sort": None,
                "chartContext": col, "offset": -1, "partitioned": False,
            })
    mts = [{"name": col, "func": func, "chartContext": col} for col, func in metrics]
    mts.append({"name": "!calc.count.0.0", "func": "CUSTOM"})
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": mts},
                "clsSupport": True,
                "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                "filterExpressions": list(filters),
                "parameters": {},
            },
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": stream_id, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": SHEET_ID, "visualId": visual_id, "visualArn": None,
            "visualType": visual_type, "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _distinct_values(
    col_id: str, stream_id: str, visual_id: str, visual_type: str,
    cookies: dict[str, str], mbtc: str, timeout: float,
) -> list[str]:
    sv = _start_vis(
        visual_id=visual_id, visual_type=visual_type, stream_id=stream_id,
        dims=[col_id], metrics=[], filters=[], limit=2000,
    )
    msg = _stream_vis(sv, cookies, mbtc, timeout)
    out: list[str] = []
    for item in (msg or {}).get("data", []):
        g = item.get("group", [])
        if g and g[0] is not None:
            out.append(str(g[0]))
    return out


def _resolve_ofd_value(
    plan_date: str, col_id: str, stream_id: str, visual_id: str, visual_type: str,
    cookies: dict[str, str], mbtc: str, timeout: float,
) -> str | None:
    """Find the ``ofd_date_str`` raw value matching ``plan_date`` (ISO).

    The column is a string whose exact format we don't control, so we pull
    the distinct values (a few weeks' worth) and match by parsed date.
    """
    try:
        target = _date.fromisoformat(plan_date[:10])
    except ValueError:
        return None
    for raw in _distinct_values(col_id, stream_id, visual_id, visual_type, cookies, mbtc, timeout):
        s = raw.strip()
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            try:
                if _date(int(m.group(1)), int(m.group(2)), int(m.group(3))) == target:
                    return raw
            except ValueError:
                continue
    return None


def _hhmm(raw: Any) -> str:
    """Extract ``HH:MM`` from a QuickSight timestamp-ish group value."""
    s = str(raw or "").strip()
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else s


def fetch_sweepers(
    *, station: str, plan_date: str, timeout: float = 30.0,
) -> dict[str, Any]:
    """Pull the Forecast Curves + VRID Details for ``station`` on ``plan_date``.

    Returns::

        {"plan_date": ..., "station": ...,
         "curves": {"series": [labels], "points": [{"t": "HH:MM", "v": [n,...]}]},
         "trucks": [{"arrival","origin","destination","vrid",
                     "equipment_type","forecast"}]}
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion.")
    cookies, mbtc = _open_session(timeout)

    # Resolve the raw ofd_date_str value for the plan date, per dataset.
    curves_ofd = _resolve_ofd_value(
        plan_date, f"{_DS_CURVES}.ofd_date_str", CURVES_STREAM_ID,
        CURVES_VISUAL_ID, "LINE_CHART", cookies, mbtc, timeout,
    )
    trucks_ofd = _resolve_ofd_value(
        plan_date, f"{_DS_TRUCKS}.ofd_date_str", TRUCKS_STREAM_ID,
        TRUCKS_VISUAL_ID, "PIVOT_TABLE", cookies, mbtc, timeout,
    )

    out: dict[str, Any] = {
        "plan_date": plan_date,
        "station": station,
        "curves": {"series": [s[0] for s in CURVE_SERIES], "points": []},
        "trucks": [],
    }

    # ── Forecast Curves ──
    if curves_ofd is not None:
        filters = [
            _filter_in(f"{_DS_CURVES}.dest_site", [station]),
            _filter_in(f"{_DS_CURVES}.ofd_date_str", [curves_ofd]),
        ]
        sv = _start_vis(
            visual_id=CURVES_VISUAL_ID, visual_type="LINE_CHART",
            stream_id=CURVES_STREAM_ID,
            dims=[f"{_DS_CURVES}.arrival_timestamp_local"],
            metrics=[(col, "MAX") for _lbl, col in CURVE_SERIES],
            filters=filters, limit=4000,
        )
        msg = _stream_vis(sv, cookies, mbtc, timeout)
        pts = []
        for item in (msg or {}).get("data", []):
            g = item.get("group", [])
            mcells = (item.get("current") or {}).get("metrics", {})
            vals = [_metric_value(mcells.get(col)) for _lbl, col in CURVE_SERIES]
            pts.append({"t": str(g[0]) if g else "", "hhmm": _hhmm(g[0] if g else ""), "v": vals})
        pts.sort(key=lambda p: p["t"])
        out["curves"]["points"] = pts

    # ── VRID Details (trucks) ──
    if trucks_ofd is not None:
        filters = [
            _filter_in(f"{_DS_TRUCKS}.dest_site", [station]),
            _filter_in(f"{_DS_TRUCKS}.ofd_date_str", [trucks_ofd]),
        ]
        sv = _start_vis(
            visual_id=TRUCKS_VISUAL_ID, visual_type="PIVOT_TABLE",
            stream_id=TRUCKS_STREAM_ID,
            dims=[col for _k, col in TRUCK_DIMS],
            metrics=[(TRUCK_METRIC, "SUM")],
            filters=filters, limit=2000,
        )
        msg = _stream_vis(sv, cookies, mbtc, timeout)
        trucks = []
        for item in (msg or {}).get("data", []):
            g = item.get("group", [])
            mcells = (item.get("current") or {}).get("metrics", {})
            rec = {k: (str(g[i]) if i < len(g) and g[i] is not None else "") for i, (k, _c) in enumerate(TRUCK_DIMS)}
            rec["hhmm"] = _hhmm(rec.get("arrival"))
            fc = _metric_value(mcells.get(TRUCK_METRIC))
            try:
                rec["forecast"] = float(fc) if fc is not None else 0.0
            except (TypeError, ValueError):
                rec["forecast"] = 0.0
            trucks.append(rec)
        trucks.sort(key=lambda r: r.get("arrival", ""))
        out["trucks"] = trucks

    out["found"] = bool(out["curves"]["points"] or out["trucks"])
    return out


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_sweepers",
    "sweepers_login_status",
]
