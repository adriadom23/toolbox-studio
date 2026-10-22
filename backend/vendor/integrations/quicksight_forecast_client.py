"""QuickSight **Forecast** integration (EU Cap & Forecast dashboard).

Pulls the four forecast metrics the PM Wave Plan needs from the
``Spain 24HR`` sheet of dashboard ``3ddfdbb0-...`` (same QuickSight
account/session as the scheduling client) and returns them per station.
The frontend synthesizes the minimal ``Deep Dive view`` CSV the existing
forecast parser already accepts.

The dataset carries hourly publication snapshots keyed by ``hour_pub``
(``AM Forecast`` / ``PM Forecast`` / ``RnF Night Forecast``) and a
``Timestamp`` publication time. We pick the **PM** snapshot (matching the
parser's PM preference) with the latest ``Timestamp`` for the plan's OFD
date, falling back to AM, then to the latest available.

Metric mapping (resolved via the visual's ``customLabels``):

* ``final_ofd``          -> "1.1 Constrained OFD Forecast" -> Volumen OFD D+1
* ``total_injected``     -> "3.4 Constrained Total Intake Volume" -> Volumen Total
* ``projected_sideline`` -> "4.1 Planned Sideline-Out" -> Sideline
* ``snop_ofd_volume``    -> "6.1 W-1 OFD Forecast" -> SnOp C1
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

DASHBOARD_ID = "3ddfdbb0-52e5-449b-a819-0a0f180e28c7"
SHEET_ID = "3ddfdbb0-52e5-449b-a819-0a0f180e28c7_f092ac35-498d-4bc2-a11c-f448a3f3b8ff"
VISUAL_ID = "3ddfdbb0-52e5-449b-a819-0a0f180e28c7_5a4f73c7-414f-452f-8c92-b1cc0191d888"
STREAM_SOURCE_ID = "707c5a06-8f45-4b96-b7d6-c688126c5594"
LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"

_P = "f65528df-75c9-40ff-a673-7e75d9fa9a60"
_C_STATION = f"{_P}.Station"
_C_OFD = f"{_P}.OFD_DAY"
_C_HOUR = f"{_P}.hour_pub"
_C_TS = f"{_P}.Timestamp"

# metric column id -> wave-plan metric label
_METRICS: dict[str, str] = {
    f"{_P}.final_ofd": "Volumen OFD D+1",
    f"{_P}.total_injected": "Volumen Total",
    f"{_P}.projected_sideline": "Sideline",
    f"{_P}.snop_ofd_volume": "SnOp C1",
    # 2.1 Earlies Expected — persisted with the WavePlan metrics so the NS
    # Flow Meeting "OFD FDD" field can read it back from the shared workbook.
    f"{_P}.earlies_expected": "Earlies Expected",
}
_GROUP = [_C_STATION, _C_OFD, _C_HOUR, _C_TS]

# Full Deep Dive metric column -> Labor Calculator forecast field.
# These are the real dataset columns of the Forecast visual (confirmed from
# the dashboard's calculated expressions, e.g. Cap Utilization =
# ``sum(Capped_Forecast)/sum(Soft_Cap)``). Streaming them explicitly is
# deterministic and avoids the fragile ``appliedAxes`` discovery that only
# resolved a handful of rows (leaving 1.2 Slam and 1.3 Cap empty).
_FIELD_METRICS: dict[str, str] = {
    f"{_P}.final_ofd": "constrained_ofd",           # 1.1 Constrained OFD
    f"{_P}.Uncapped_Forecast": "unconstrained_slam",  # 1.2 Unconstrained Slam
    f"{_P}.Soft_Cap": "new_intake_cap",             # 1.3 New Intake Cap
    f"{_P}.Capped_Forecast": "capped_slam",         # 1.4 Capped Slam Forecast
    f"{_P}.earlies_expected": "earlies_expected",   # 2.1
    f"{_P}.earlies_received": "earlies_received",   # 2.2
    f"{_P}.mnr_expected": "mnr_expected",           # 2.3
    f"{_P}.mnr_received": "mnr_received",            # 2.4
    f"{_P}.returns_to_station": "returns",          # 3.2
    f"{_P}.sideline_in": "sideline_in",             # 3.3
    f"{_P}.Volume_Lost_Cap": "volume_lost_cap",     # 1.5 Volume lost due to Caps
    f"{_P}.projected_sideline": "planned_sideout",  # 4.1
    f"{_P}.required_sideline": "required_sideout",  # 4.2
    f"{_P}.utr_capacity": "utr_capacity",           # 5.1
    f"{_P}.otr_capacity": "otr_capacity",           # 5.2
    f"{_P}.mech_capacity": "mech_capacity",         # 5.3
    f"{_P}.snop_ofd_volume": "snop_forecast",       # 6.1 W-1 OFD (SnOP)
}

# ---------------------------------------------------------------------------
# 48HR forecast (Spain 48HR+ sheet). It is a *separate* dataset with the same
# schema, served by its own sheet/visual/stream source. Confirmed from the
# 48HR HAR: the station filter on that sheet targets visual ``894f1b1a`` with
# preparedDataSourceId ``74646100`` -> dataset ``559e3973``.
# ---------------------------------------------------------------------------
_P48 = "559e3973-d21e-4fb7-aa6a-4aad9f767ef3"
SHEET_ID_48 = "3ddfdbb0-52e5-449b-a819-0a0f180e28c7_129c9ae7-b762-4027-b5a2-5c9eca57be1b"
VISUAL_ID_48 = "3ddfdbb0-52e5-449b-a819-0a0f180e28c7_894f1b1a-701b-4271-97cf-05d724f32b6a"
STREAM_SOURCE_ID_48 = "74646100-dd11-4b82-91b7-f5cb46f146a2"
_C48_STATION = f"{_P48}.Station"
_C48_OFD = f"{_P48}.OFD_DAY"
_C48_HOUR = f"{_P48}.hour_pub"
_C48_TS = f"{_P48}.Timestamp"
_GROUP48 = [_C48_STATION, _C48_OFD, _C48_HOUR, _C48_TS]
_FIELD_METRICS_48 = {f"{_P48}.{col.split('.', 1)[1]}": field for col, field in _FIELD_METRICS.items()}

# Metric label -> the Deep Dive metric-row label the parser maps back.
DEEP_DIVE_ROW_LABELS: dict[str, str] = {
    "Volumen OFD D+1": "1.1 Constrained OFD Forecast",
    "Earlies Expected": "2.1 Earlies Expected",
    "Volumen Total": "3.4 Constrained Total Intake Volume",
    "Sideline": "4.1 Planned Sideline-Out",
    "SnOp C1": "6.1 W-1 OFD Forecast",
}

# ---------------------------------------------------------------------------
# Deep Dive full extraction (Labor Calculator).
#
# The Forecast dashboard's Deep Dive pivot exposes ~30 metric rows numbered
# ``1.1``..``6.4`` (see the dashboard's row headers). They are VALUE-axis
# columns of the same visual we already stream, so we discover them from the
# sheet metadata (``GetSheetForDashboard`` -> ``appliedAxes``) and map each to
# the Labor Calculator field by its bucket number, falling back to keyword
# matching on the physical column name.
# ---------------------------------------------------------------------------

# Deep Dive bucket number -> Labor Calculator forecast field.
_BUCKET_FIELD: dict[str, str] = {
    "1.1": "constrained_ofd",      # 1.1 OFD Forecast
    "1.2": "unconstrained_slam",   # 1.2 Uncapped Slam Forecast
    "1.3": "new_intake_cap",       # 1.3 New Intake Cap
    "2.1": "earlies_expected",     # 2.1 Earlies Expected
    "2.2": "earlies_received",     # 2.2 Earlies Received
    "2.3": "mnr_received",         # 2.3 MNR Received
    "2.4": "mnr_expected",         # 2.4 MNR Expected
    "3.1": "backlog",              # 3.1 Backlog
    "3.2": "returns",              # 3.2 Returns
    "3.3": "sideline_in",          # 3.3 Sideline In
    "4.1": "planned_sideout",      # 4.1 Planned Sideline-Out
    "5.1": "utr_capacity",         # 5.1 UTR Capacity
    "5.2": "otr_capacity",         # 5.2 OTR Capacity
    "5.3": "mech_capacity",        # 5.3 Mechanical Capacity
    "5.5": "vol_lost_utr_scenario",  # 5.5 Volume lost due to UTR
    "5.7": "vol_lost_otr_scenario",  # 5.7 Volume lost due to OTR
    "6.1": "snop_forecast",        # 6.1 W-1 OFD Forecast
}

# Physical column-name keyword -> field (fallback when the pivot row label is
# not exposed on the axis and we only get the dataset column name).
_NAME_FIELD: list[tuple[str, str]] = [
    ("final_ofd", "constrained_ofd"),
    ("uncapped_slam", "unconstrained_slam"),
    ("unconstrained_slam", "unconstrained_slam"),
    ("uncapped_forecast", "unconstrained_slam"),  # real column name
    ("soft_cap", "new_intake_cap"),               # real column name (the cap)
    ("new_intake_cap", "new_intake_cap"),
    ("intake_cap", "new_intake_cap"),
    ("earlies_expected", "earlies_expected"),
    ("earlies_received", "earlies_received"),
    ("mnr_received", "mnr_received"),
    ("mnr_expected", "mnr_expected"),
    ("backlog", "backlog"),
    ("returns", "returns"),
    ("sideline_in", "sideline_in"),
    ("planned_sideline", "planned_sideout"),
    ("projected_sideline", "planned_sideout"),
    ("utr_capacity", "utr_capacity"),
    ("otr_capacity", "otr_capacity"),
    ("mechanical_capacity", "mech_capacity"),
    ("mechnical_capacity", "mech_capacity"),
    ("mech_capacity", "mech_capacity"),
    ("snop_ofd", "snop_forecast"),
    ("w_1_ofd", "snop_forecast"),
]

_BUCKET_RE = re.compile(r"(\d\.\d)")


def _label_to_field(label: str, name: str) -> str | None:
    """Resolve a metric axis to a Labor Calculator field.

    Prefer the Deep Dive bucket number in the display label (``1.1``..``6.1``);
    fall back to keyword matching on the physical column name."""
    m = _BUCKET_RE.search(str(label or ""))
    if m and m.group(1) in _BUCKET_FIELD:
        return _BUCKET_FIELD[m.group(1)]
    n = str(name or "").lower()
    for token, field in _NAME_FIELD:
        if token in n:
            return field
    return None


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


def _build_start_vis(
    metric_ids: list[str] | None = None,
    *,
    metric_aggs: dict[str, str] | None = None,
    group: list[str] | None = None,
    sort_col: str | None = None,
    stream_source_id: str | None = None,
    sheet_id: str | None = None,
    visual_id: str | None = None,
) -> dict[str, Any]:
    metric_ids = list(metric_ids) if metric_ids else list(_METRICS)
    group = list(group) if group else list(_GROUP)
    sort_col = sort_col or _C_OFD
    stream_source_id = stream_source_id or STREAM_SOURCE_ID
    sheet_id = sheet_id or SHEET_ID
    visual_id = visual_id or VISUAL_ID
    last = len(group) - 1
    fields = []
    for i, cid in enumerate(group):
        if i == last:
            fields.append({"name": cid, "limit": 50000,
                           "sort": {"name": sort_col, "dir": "DESC"},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    metric_aggs = metric_aggs or {}
    metrics = [
        {
            "name": cid,
            "func": str(metric_aggs.get(cid) or "SUM").strip().upper(),
            "chartContext": cid,
        }
        for cid in metric_ids
    ]
    metrics.append({"name": "!calc.count.0.0", "func": "CUSTOM"})
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {"excludeCountStar": True, "group": {"fields": fields, "metrics": metrics},
                    "clsSupport": True,
                    "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                    "filterExpressions": [], "parameters": {}},
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": stream_source_id, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": sheet_id, "visualId": visual_id, "visualArn": None,
            "visualType": "TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def forecast_login_status() -> dict[str, Any]:
    """Whether the session is live for the *Forecast* dashboard (``3ddfdbb0``).

    The Labor Calculator reads from the Forecast dashboard, so the
    session must be validated against it — not the Scheduling (or DPR)
    dashboard. Validating against a dashboard the account cannot see
    returns HTTP 404 and would report ``session_expired`` with a
    perfectly valid session.
    """
    from app.integrations.quicksight_scheduling_client import dashboard_login_status

    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def _pick_snapshot(rows: list[dict[str, Any]], prefer: str = "PM") -> dict[str, Any] | None:
    """From one station+date's rows pick the preferred publication snapshot.

    ``prefer`` selects which ``hour_pub`` publication wins when several exist:
    ``"PM"`` (wave-plan default) ranks PM > AM > other; ``"AM"`` (Labor
    Calculator morning plan) ranks AM > PM > other. A **Reforecast** (hour_pub
    contains ``SIM`` / ``REFOR``) always wins over everything else. Ties break
    on the latest ``Timestamp`` (the most recent publication)."""
    if not rows:
        return None
    prefer = (prefer or "PM").upper()

    def order(r):
        hp = str(r.get("_hour") or "").upper()
        # A reforecast is a re-publication and must always be taken (latest).
        if "SIM" in hp or "REFOR" in hp:
            return (10, str(r.get("_ts") or ""))
        if prefer in ("48", "48HR", "48H"):
            # 48-hour-ahead publication (hour_pub contains "48").
            rank = 2 if "48" in hp else (1 if "AM" in hp else 0)
        elif prefer == "AM":
            rank = 2 if "AM" in hp else (1 if "PM" in hp else 0)
        else:
            rank = 2 if "PM" in hp else (1 if "AM" in hp else 0)
        return (rank, str(r.get("_ts") or ""))

    return sorted(rows, key=order)[-1]


def _open_session(timeout: float = 35.0) -> tuple[dict[str, str], str]:
    """Read cookies + CSRF token and warm up the forecast dashboard once."""
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


def _extract_station_metrics(
    data: dict[str, Any], station: str, plan_date: str | None
) -> dict[str, float]:
    """Pull one station's latest PM snapshot out of an already-streamed frame."""
    station = (station or "").strip().upper()
    plan_date = (plan_date or "").strip() or None
    metric_ids = list(_METRICS)
    candidates: list[dict[str, Any]] = []
    for item in data.get("data", []):
        g = item.get("group", [])
        st = str(g[0]).strip().upper() if g else ""
        if st != station:
            continue
        ofd = str(g[1])[:10] if len(g) > 1 else ""
        if plan_date and ofd != plan_date:
            continue
        mcells = (item.get("current") or {}).get("metrics", {})
        rec: dict[str, Any] = {"_hour": g[2] if len(g) > 2 else "", "_ts": g[3] if len(g) > 3 else ""}
        for mid in metric_ids:
            rec[_METRICS[mid]] = _metric_value(mcells.get(mid))
        candidates.append(rec)
    best = _pick_snapshot(candidates)
    if not best:
        return {}
    return {label: float(best.get(label) or 0) for label in _METRICS.values()}


def fetch_forecast_metrics(
    *, station: str, plan_date: str | None = None, timeout: float = 35.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
) -> dict[str, float]:
    """Return ``{metric_label: value}`` for ``station`` on ``plan_date``.

    Picks the latest PM publication snapshot. Empty dict when the station
    has no forecast rows for that date."""
    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    data = _stream_vis(_build_start_vis(), cookies, mbtc, timeout)
    if not data:
        raise QuickSightError("QuickSight no devolvio datos de Forecast.")
    return _extract_station_metrics(data, station, plan_date)


def fetch_forecast_metrics_bulk(
    *, stations: list[str], plan_date: str | None = None, timeout: float = 35.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
) -> dict[str, dict[str, float]]:
    """Return ``{station: {metric_label: value}}`` for many stations at once.

    The forecast visual already streams every station's rows in a single
    frame, so we download it **once** and slice per station instead of
    re-pulling the whole table for each one. Stations without rows for the
    date are omitted."""
    stations = [s.strip().upper() for s in (stations or []) if s and s.strip()]
    if not stations:
        return {}
    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    data = _stream_vis(_build_start_vis(), cookies, mbtc, timeout)
    if not data:
        raise QuickSightError("QuickSight no devolvio datos de Forecast.")

    out: dict[str, dict[str, float]] = {}
    for st in stations:
        metrics = _extract_station_metrics(data, st, plan_date)
        if metrics:
            out[st] = metrics
    return out


def _fetch_sheet_json(
    cookies: dict[str, str],
    mbtc: str,
    timeout: float,
    *,
    sheet_id: str = SHEET_ID,
) -> dict[str, Any] | None:
    """GET ``GetSheetForDashboard`` and return the parsed JSON (visual layout)."""
    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{DASHBOARD_ID}/sheets/{sheet_id}"
        f"?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA, "Accept": "*/*", "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc, "x-amzn-target": "api", "Referer": _page_url(),
    }
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=timeout, allow_redirects=False)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _discover_metric_axes(
    sheet_json: dict[str, Any],
    *,
    visual_id: str = VISUAL_ID,
) -> list[dict[str, Any]]:
    """Walk one visual layout and return its VALUE-axis metrics.

    Each entry is ``{"id", "label", "name", "agg"}`` — ``label`` is the pivot
    row header (e.g. ``1.1 OFD Forecast``) when the axis carries a custom
    label, else the physical column name."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("visualId") == visual_id and "appliedAxes" in node:
                for ax in node.get("appliedAxes") or []:
                    if not isinstance(ax, dict):
                        continue
                    axis = str(ax.get("axisName") or "").upper()
                    if axis in ("VALUE", "VALUES", "METRIC", "METRICS"):
                        cid = ax.get("columnId")
                        if not cid:
                            continue
                        labels = ax.get("customLabels") or {}
                        plural_label = (
                            next((str(value) for value in labels.values() if value), "")
                            if isinstance(labels, dict)
                            else ""
                        )
                        found.append({
                            "id": cid,
                            "label": plural_label or ax.get("customLabel") or ax.get("label") or ax.get("columnName") or "",
                            "name": ax.get("columnName") or "",
                            "agg": (ax.get("aggregationFunction") or "SUM"),
                        })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(sheet_json)
    return found


def _latest_ofd_for_station(data: dict[str, Any], station: str) -> str | None:
    """Most recent OFD date (``YYYY-MM-DD``) present for ``station``.

    Used to resolve a concrete target date for the 48HR fetch when the
    caller did not pass one, so we never let a SIM reforecast published
    for an unrelated day win the global snapshot pick."""
    station = (station or "").strip().upper()
    dates: set[str] = set()
    for item in data.get("data", []):
        g = item.get("group", [])
        if g and str(g[0]).strip().upper() == station and len(g) > 1:
            ofd = str(g[1])[:10]
            if ofd:
                dates.add(ofd)
    return max(dates) if dates else None


def _extract_station_raw(
    data: dict[str, Any], station: str, plan_date: str | None, metric_ids: list[str],
    prefer: str = "AM",
) -> dict[str, float]:
    """Pull one station's preferred snapshot as ``{columnId: value}``.

    Defaults to the AM publication with the most recent ``Timestamp`` (the
    Labor Calculator reads the morning forecast)."""
    station = (station or "").strip().upper()
    plan_date = (plan_date or "").strip() or None
    candidates: list[dict[str, Any]] = []
    for item in data.get("data", []):
        g = item.get("group", [])
        st = str(g[0]).strip().upper() if g else ""
        if st != station:
            continue
        ofd = str(g[1])[:10] if len(g) > 1 else ""
        if plan_date and ofd != plan_date:
            continue
        mcells = (item.get("current") or {}).get("metrics", {})
        rec: dict[str, Any] = {"_hour": g[2] if len(g) > 2 else "", "_ts": g[3] if len(g) > 3 else ""}
        for mid in metric_ids:
            value = _metric_value(mcells.get(mid))
            if value is not None:
                rec[mid] = value
        candidates.append(rec)
    best = _pick_snapshot(candidates, prefer=prefer)
    if not best:
        return {}
    return {
        mid: float(best[mid])
        for mid in metric_ids
        if mid in best and best[mid] is not None
    }


def _fetch_discovered_fields(
    *,
    station: str,
    plan_date: str | None,
    cookies: dict[str, str],
    mbtc: str,
    timeout: float,
    prefer: str,
    wanted_fields: set[str] | None = None,
    group: list[str] | None = None,
    sort_col: str | None = None,
    stream_source_id: str | None = None,
    sheet_id: str = SHEET_ID,
    visual_id: str = VISUAL_ID,
) -> dict[str, float]:
    """Fetch fields identified from the visual's bucket labels.

    Some calculated rows, notably 5.5 and 5.7, do not have stable physical
    dataset names. Their QuickSight custom labels are the source of truth, so
    this path supplements the deterministic field list without guessing IDs.
    """
    sheet = _fetch_sheet_json(cookies, mbtc, timeout, sheet_id=sheet_id)
    axes = _discover_metric_axes(sheet, visual_id=visual_id) if sheet else []
    id_to_field: dict[str, str] = {}
    id_to_agg: dict[str, str] = {}
    for ax in axes:
        field = _label_to_field(ax["label"], ax["name"])
        if wanted_fields is not None and field not in wanted_fields:
            continue
        if field and ax["id"] not in id_to_field:
            id_to_field[ax["id"]] = field
            id_to_agg[ax["id"]] = str(ax.get("agg") or "SUM")

    if not id_to_field:
        return {}

    metric_ids = list(id_to_field)
    data = _stream_vis(
        _build_start_vis(
            metric_ids,
            metric_aggs=id_to_agg,
            group=group,
            sort_col=sort_col,
            stream_source_id=stream_source_id,
            sheet_id=sheet_id,
            visual_id=visual_id,
        ),
        cookies,
        mbtc,
        timeout,
    )
    if not data:
        return {}

    raw = _extract_station_raw(data, station, plan_date, metric_ids, prefer=prefer)
    return {
        id_to_field[mid]: value
        for mid, value in raw.items()
        if mid in id_to_field
    }


def _fetch_optional_cap_loss_fields(**kwargs: Any) -> dict[str, float]:
    """Best-effort 5.5/5.7 supplement without degrading the base forecast.

    Keep the optional metadata + WebSocket pass on a small budget. The base
    forecast has already succeeded, so no failure from this supplement may
    turn the whole request into an error.
    """
    optional_args = dict(kwargs)
    optional_args["timeout"] = min(float(optional_args.get("timeout") or 5.0), 5.0)
    try:
        return _fetch_discovered_fields(
            wanted_fields={"vol_lost_utr_scenario", "vol_lost_otr_scenario"},
            **optional_args,
        )
    except Exception:  # noqa: BLE001 - deliberately isolated optional data
        return {}


def fetch_forecast_fields(
    *, station: str, plan_date: str | None = None, timeout: float = 40.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
    prefer: str = "AM",
) -> dict[str, float]:
    """Return ``{labor_field: value}`` for the full Deep Dive forecast.

    Discovers every VALUE-axis metric of the forecast visual, streams them in
    one shot and maps each to a Labor Calculator field (by bucket number, else
    physical column name). Falls back to the 4 hardcoded metrics when the
    dynamic discovery yields nothing so the import never returns empty.

    ``prefer`` selects the publication snapshot: ``"AM"`` for the D+1 morning
    forecast (default) or ``"48"`` for the 48-hour-ahead (48HR) publication."""
    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    # The 48HR forecast lives on a separate dataset/sheet/visual with the same
    # schema. Route to it explicitly; the D+1 stream never contains 48HR data.
    is_48 = str(prefer).upper() in ("48", "48HR", "48H")

    if is_48:
        # The 48HR dataset holds MANY OFD dates at once (and sometimes a SIM
        # reforecast published for an unrelated day). We must pin the target
        # OFD, otherwise the global snapshot pick lets another day's row win
        # (e.g. a stray SIM reforecast), which reads the wrong forecast.
        metric_ids48 = list(_FIELD_METRICS_48)
        vis48 = _build_start_vis(
            metric_ids48, group=_GROUP48, sort_col=_C48_OFD,
            stream_source_id=STREAM_SOURCE_ID_48, sheet_id=SHEET_ID_48, visual_id=VISUAL_ID_48,
        )
        data48 = _stream_vis(vis48, cookies, mbtc, timeout)
        if data48:
            target = (plan_date or "").strip() or None
            selected_target = target
            raw = _extract_station_raw(data48, station, target, metric_ids48, prefer="48")
            # If the requested OFD has no 48HR row yet (not published), fall
            # back to the latest available OFD for the station — never to a
            # date-agnostic pick that a SIM reforecast could hijack.
            if not raw:
                latest = _latest_ofd_for_station(data48, station)
                if latest and latest != target:
                    selected_target = latest
                    raw = _extract_station_raw(data48, station, latest, metric_ids48, prefer="48")
            out: dict[str, float] = {}
            for mid, value in raw.items():
                field = _FIELD_METRICS_48.get(mid)
                if field:
                    out[field] = value
            if out:
                discovered = _fetch_optional_cap_loss_fields(
                    station=station,
                    plan_date=selected_target,
                    cookies=cookies,
                    mbtc=mbtc,
                    timeout=timeout,
                    prefer="48",
                    group=_GROUP48,
                    sort_col=_C48_OFD,
                    stream_source_id=STREAM_SOURCE_ID_48,
                    sheet_id=SHEET_ID_48,
                    visual_id=VISUAL_ID_48,
                )
                out.update(discovered)
                return out
        raise QuickSightError(
            f"QuickSight no devolvio datos 48HR para {station}"
            + (f" en {plan_date}." if plan_date else ".")
        )

    # Primary: stream the known Deep Dive metric columns explicitly. This is
    # deterministic (same visual/stream the 4 wave-plan metrics already use)
    # and reliably fills 1.2 Unconstrained Slam and 1.3 New Intake Cap, which
    # the calculated Cap Utilization (Capped_Forecast / Soft_Cap) depends on.
    metric_ids = list(_FIELD_METRICS)
    data = _stream_vis(_build_start_vis(metric_ids), cookies, mbtc, timeout)
    out: dict[str, float] = {}
    if data:
        raw = _extract_station_raw(data, station, plan_date, metric_ids, prefer=prefer)
        for mid, value in raw.items():
            field = _FIELD_METRICS.get(mid)
            if field:
                out[field] = value
        if out:
            # Buckets 5.5/5.7 are calculated rows whose physical IDs are not
            # stable. Supplement the known columns from their visual labels.
            discovered = _fetch_optional_cap_loss_fields(
                station=station,
                plan_date=plan_date,
                cookies=cookies,
                mbtc=mbtc,
                timeout=timeout,
                prefer=prefer,
            )
            out.update(discovered)
            return out

    # Secondary: discover VALUE-axis metrics from the sheet layout and map by
    # bucket number / physical column name.
    discovered = _fetch_discovered_fields(
        station=station,
        plan_date=plan_date,
        cookies=cookies,
        mbtc=mbtc,
        timeout=timeout,
        prefer=prefer,
    )
    if discovered:
        return discovered

    # Fallback: the 4 always-known metrics mapped onto their fields.
    metrics = fetch_forecast_metrics(
        station=station, plan_date=plan_date, timeout=timeout, cookies=cookies, mbtc=mbtc,
    )
    label_field = {
        "Volumen OFD D+1": "constrained_ofd",
        "Sideline": "planned_sideout",
        "SnOp C1": "snop_forecast",
    }
    return {label_field[k]: v for k, v in metrics.items() if k in label_field}


__all__ = [
    "DASHBOARD_ID",
    "DEEP_DIVE_ROW_LABELS",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_forecast_fields",
    "fetch_forecast_metrics",
    "fetch_forecast_metrics_bulk",
    "forecast_login_status",
]
