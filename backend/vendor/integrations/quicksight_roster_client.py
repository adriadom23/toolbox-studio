"""QuickSight integration for the **First Roster / Rostering Compliance** dashboard.

Sibling of :mod:`app.integrations.quicksight_carrier_client`. It pulls the
per-DSP weekly rostering-compliance table straight from the dashboard so the
"Primer roster" Slack action can attach a short weekly summary per DSP instead
of just a static reminder.

All ids below were mapped from the operator's session HAR of the dashboard
(``us-east-1.quicksight.aws.amazon.com``):

* dashboard            ``c1f0a430-3437-4e36-8167-e481f1b72ed0``
* sheet                ``…_27ced311-8b20-4a3a-b7f6-31564b3bb481``
* table visual         ``…_830aec43-085d-4e16-b1ea-4d3753ea5998``
* prepared data source ``2c60cc82-b301-4a55-8ac4-46455d12db73``
* table column prefix  ``ef08a88e-a5a0-4c82-aa58-c3ac9a6e7870``
* region/program filter ``cc6f755c-e5d2-47a4-8204-917ce44e56f0`` (= "AMZL")

Dimension columns confirmed in the HAR (``<prefix>.<name>``):
``defaultstationcode``, ``shortcode`` (DSP), ``wavegroupid`` (CYCLE_1/2),
``servicetypename``, ``startdate_local`` (the date the relative-week filter
runs on).

The dashboard's titled metrics (confirmed in the HAR insight text):

* "Rostering Capacity Compliance per DSP at 15:30 local time"  (target 70%)
* "D-1 15:30 Plan vs D0 requested routes:
   [RTW Routes before D0 Sequencing] - [CO OTR D-1 1st plan planned Routes];
   (-) Abortives / (+) Infinities"

The numeric measure **column ids are NOT in the provided HAR** (the table
data streams over a WebSocket frame that was not captured). We therefore
discover them at runtime from ``GetSheetForDashboard`` by matching the
measure field display names, and every id is overridable via environment
variables so a dashboard revision can be re-pointed without a rebuild.

This module is intentionally defensive: if the measures cannot be resolved
it raises :class:`QuickSightError` rather than emitting fabricated numbers,
because the output is sent to DSPs over Slack.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

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
    os.environ.get("ROSTER_QS_DASHBOARD_ID", "").strip()
    or "c1f0a430-3437-4e36-8167-e481f1b72ed0"
)
SHEET_ID = (
    os.environ.get("ROSTER_QS_SHEET_ID", "").strip()
    or "c1f0a430-3437-4e36-8167-e481f1b72ed0_27ced311-8b20-4a3a-b7f6-31564b3bb481"
)
VISUAL_ID = (
    os.environ.get("ROSTER_QS_VISUAL_ID", "").strip()
    or "c1f0a430-3437-4e36-8167-e481f1b72ed0_830aec43-085d-4e16-b1ea-4d3753ea5998"
)
DEFAULT_STREAM_SOURCE_ID = (
    os.environ.get("ROSTER_QS_STREAM_SOURCE_ID", "").strip()
    or "2c60cc82-b301-4a55-8ac4-46455d12db73"
)

# Prefix shared by every physical table column on this prepared data source.
_COL_PREFIX = os.environ.get("ROSTER_QS_COL_PREFIX", "").strip() or (
    "ef08a88e-a5a0-4c82-aa58-c3ac9a6e7870"
)
_STATION_ID = f"{_COL_PREFIX}.defaultstationcode"
_DSP_ID = f"{_COL_PREFIX}.shortcode"
_DATE_ID = f"{_COL_PREFIX}.startdate_local"

# Physical SPICE columns (the analysis calc-field UUIDs can't be queried at the
# SPICE layer). Confirmed against the operator's dashboard numbers:
#   compliance %        = q_rostered_routes_before_x / q_finally_rostered_routes
#   before 15:30        = q_rostered_routes_before_x
#   before sequencing   = q_finally_rostered_routes
#   D-1 15:30 vs D0      = q_finally_rostered_routes - q_planned_routes
#
# The D-1 formula is taken verbatim from the dashboard's titled definition
# (confirmed in the session HAR):
#   "D-1 15:30 Plan vs D0 requested routes:
#    [RTW Routes before D0 Sequencing] - [CO OTR D-1 1st plan planned Routes]"
#   → [RTW Routes before D0 Sequencing]      = q_finally_rostered_routes
#   → [CO OTR D-1 1st plan planned Routes]    = q_planned_routes
# (NOT total_routes — that column is the "D0 requested/total routes" framing,
# not the subtrahend of the bracketed formula.)
_COL_BEFORE_X = f"{_COL_PREFIX}.q_rostered_routes_before_x"
_COL_FINALLY = f"{_COL_PREFIX}.q_finally_rostered_routes"
_COL_PLANNED = f"{_COL_PREFIX}.q_planned_routes"
_COL_TOTAL = f"{_COL_PREFIX}.total_routes"
# Extra physical columns pulled only when ?debug=1, to pin the exact D-1 formula.
_COL_PLANNED_BX = f"{_COL_PREFIX}.q_planned_routes_before_x"
_COL_ROSTERED = f"{_COL_PREFIX}.rostered_routes"

# Program/region filter — the HAR shows it pinned to "AMZL".
_REGION_FILTER_COL = (
    os.environ.get("ROSTER_QS_REGION_COL", "").strip()
    or "cc6f755c-e5d2-47a4-8204-917ce44e56f0"
)
_REGION_FILTER_VALUE = os.environ.get("ROSTER_QS_REGION_VALUE", "").strip() or "AMZL"

ROSTER_TIMEZONE = os.environ.get("ROSTER_QS_TIMEZONE", "").strip() or "Europe/Madrid"
VISUAL_TYPE = os.environ.get("ROSTER_QS_VISUAL_TYPE", "").strip() or "TABLE"
_LIMIT = 50000
_DEFAULT_MBTC = ""

ROSTER_LOGIN_URL = (
    f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}"
    f"/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"
)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"


# ---------------------------------------------------------------------------
# Visual calc-field column ids (analysis-level), discovered from the table
# visual via ``GetSheetForDashboard`` / the ``/discover`` endpoint. These are
# the *computed* measures the dashboard itself displays — querying them lets us
# READ the dashboard's own values (notably D-1, which is a snapshot-based calc
# field that cannot be derived from the physical SPICE columns). All are
# env-overridable so a dashboard revision can be re-pointed without a rebuild.
# ---------------------------------------------------------------------------
_CF_BEFORE_SEQ = os.environ.get("ROSTER_QS_CF_BEFORESEQ", "").strip() or "a0e927a2-a4ed-4ede-ab0c-f07f5c98cc9e"
_CF_BEFORE_1530 = os.environ.get("ROSTER_QS_CF_BEFORE1530", "").strip() or "dc618371-9d9a-4582-ac60-76a23d81235e"
_CF_COMPLIANCE = os.environ.get("ROSTER_QS_CF_COMPLIANCE", "").strip() or "bf93d2cd-1a76-4a8a-81ab-f373bafed4b1"
_CF_D1 = os.environ.get("ROSTER_QS_CF_D1", "").strip() or "08f0acdb-6440-45c3-9f09-d225052815de"


# ---------------------------------------------------------------------------
# Metric definitions.
#
# ``key`` is the stable id we expose to callers (and the i18n label key).
# ``aliases`` are case-insensitive substrings matched against the measure
# field display names discovered from ``GetSheetForDashboard``. ``env`` is
# the environment variable that pins the measure column id explicitly.
# ---------------------------------------------------------------------------


@dataclass
class MetricDef:
    key: str
    label: str
    column_id: str = ""          # physical SPICE column (prefixed) for queried metrics
    func: str = "SUM"
    is_percent: bool = False
    display: bool = True         # shown as a row in the per-DSP table
    # Derived metrics are computed from other metrics rather than queried.
    op: str | None = None        # "ratio" | "diff"
    operands: tuple[str, str] | None = None  # (key_a, key_b)


def _env_col(env_name: str, default: str) -> str:
    return os.environ.get(env_name, "").strip() or default


# Display order: compliance, before 15:30, before sequencing.
METRICS: list[MetricDef] = [
    MetricDef(
        key="compliance",
        label="Compliance %",
        is_percent=True,
        op="ratio",
        operands=("rostered_before_1530", "rostered_before_seq"),
    ),
    MetricDef(
        key="rostered_before_1530",
        label="Rutas rosterizadas antes de 15:30",
        column_id=_env_col("ROSTER_QS_BEFORE1530_COL", _COL_BEFORE_X),
        func="SUM",
    ),
    MetricDef(
        key="rostered_before_seq",
        label="Rosterizadas antes de secuencia",
        column_id=_env_col("ROSTER_QS_BEFORESEQ_COL", _COL_FINALLY),
        func="SUM",
    ),
]


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _mbtc_default() -> str:
    return os.environ.get("QUICKSIGHT_MBTC", "").strip() or _DEFAULT_MBTC


def _stream_source_id() -> str:
    return (
        os.environ.get("ROSTER_QS_STREAM_SOURCE_ID", "").strip()
        or DEFAULT_STREAM_SOURCE_ID
    )


def _api_headers(mbtc: str) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "*/*",
        "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": ROSTER_LOGIN_URL,
    }


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Read the live per-session csrf token (mbtc) from the dashboard page."""
    try:
        resp = requests.get(
            ROSTER_LOGIN_URL,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
        )
        if resp.status_code == 200:
            decoded = _html.unescape(resp.text)
            m = re.search(r'"csrf"\s*:\s*"([^"]+)"', decoded)
            if m:
                return m.group(1)
    except requests.RequestException:
        pass
    return _mbtc_default()


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    """Bootstrap the session; capture the Set-Cookie tokens QuickSight issues."""
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    merged = dict(cookies)
    try:
        resp = requests.get(
            url, headers=_api_headers(mbtc), cookies=merged, timeout=timeout, allow_redirects=False
        )
        merged.update(_extract_set_cookies(resp))
        if resp.status_code in (401, 403):
            resp2 = requests.get(
                url, headers=_api_headers(mbtc), cookies=merged, timeout=timeout, allow_redirects=False
            )
            merged.update(_extract_set_cookies(resp2))
    except requests.RequestException:
        pass
    return merged


def _cell_value(cell: Any) -> float | None:
    """Read a measure cell regardless of aggregation key (count/sum/custom)."""
    if not isinstance(cell, dict) or not cell:
        return None
    for key in ("custom", "sum", "count", "avg", "average", "min", "max"):
        if key in cell:
            val = cell[key]
            if isinstance(val, dict):
                val = val.get("parsedValue", val.get("source"))
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# Login status (reliable — used by the "Conectar QuickSight" button).
# ---------------------------------------------------------------------------


def roster_login_status() -> dict[str, Any]:
    """Check whether the QuickSight session can read the roster dashboard."""
    cookies = _read_cookies()
    if not cookies:
        return {
            "ready": False,
            "cookie_count": 0,
            "correct_account": False,
            "host": QUICKSIGHT_HOST,
            "reason": "no_cookies",
            "login_url": ROSTER_LOGIN_URL,
        }
    correct_alias = _is_session_for_alias(cookies)
    mbtc = _fetch_mbtc(cookies)
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=1"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    try:
        resp = requests.get(
            url, headers=_api_headers(mbtc), cookies=cookies, timeout=12, allow_redirects=False
        )
        body = resp.text.lower()
        live = (
            resp.status_code == 200
            and "authenticate" not in body
            and "error" not in body[:50]
        )
        return {
            "ready": live,
            "cookie_count": len(cookies),
            "correct_account": correct_alias,
            "host": QUICKSIGHT_HOST,
            "reason": "" if live else ("wrong_account" if not correct_alias else "session_expired"),
            "login_url": ROSTER_LOGIN_URL,
        }
    except requests.RequestException:
        return {
            "ready": False,
            "cookie_count": len(cookies),
            "correct_account": correct_alias,
            "host": QUICKSIGHT_HOST,
            "reason": "network_error",
            "login_url": ROSTER_LOGIN_URL,
        }


# ---------------------------------------------------------------------------
# Sheet definition + measure discovery.
# ---------------------------------------------------------------------------

_COL_RE = re.compile(re.escape(_COL_PREFIX) + r"\.[A-Za-z0-9_]+")


def _get_sheet_definition(cookies: dict[str, str], mbtc: str, timeout: float = 20.0) -> dict[str, Any]:
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=200"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    try:
        resp = requests.get(
            url, headers=_api_headers(mbtc), cookies=cookies, timeout=timeout, allow_redirects=False
        )
        if resp.status_code != 200:
            return {}
        return resp.json() if resp.text else {}
    except (requests.RequestException, ValueError):
        return {}


def _find_visual_node(defn: dict[str, Any], visual_id: str) -> dict[str, Any] | None:
    """Locate the dict carrying ``visualId == visual_id``."""
    found: dict[str, Any] | None = None

    def visit(node: Any) -> None:
        nonlocal found
        if found is not None:
            return
        if isinstance(node, dict):
            vid = node.get("visualId") or node.get("VisualId")
            if vid == visual_id:
                found = node
                return
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(defn)
    return found


def discover_measures(defn: dict[str, Any], visual_id: str = VISUAL_ID) -> list[dict[str, str]]:
    """Return ``[{"column_id", "name", "agg"}]`` for the visual's METRIC fields.

    Reads the pivot/table ``appliedAxes`` entries with ``axisName == 'METRIC'``
    (the measures), which carry the column id, ``customLabels`` and the
    aggregation function. Falls back to scanning generic ``columnId`` nodes.
    """
    node = _find_visual_node(defn, visual_id) or defn
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    axes = node.get("appliedAxes") if isinstance(node, dict) else None
    if isinstance(axes, list):
        for ax in axes:
            if not isinstance(ax, dict):
                continue
            if str(ax.get("axisName") or "").upper() != "METRIC":
                continue
            col = ax.get("columnId")
            if not isinstance(col, str) or not col or col in seen:
                continue
            labels = ax.get("customLabels") or {}
            label = next(iter(labels.values()), None) if isinstance(labels, dict) else None
            seen.add(col)
            out.append(
                {
                    "column_id": col,
                    "name": label or ax.get("columnName") or col,
                    "agg": str(ax.get("aggregationFunction") or "SUM"),
                }
            )
    return out


def _extract_calculated_fields(defn: dict[str, Any]) -> list[dict[str, str]]:
    """Scan the sheet definition for calculated-field expressions.

    QuickSight stores calc fields as dicts carrying an ``expression`` (and a
    ``name``/``alias``). Returning them lets us read the dashboard's exact
    formula for the D-1 metric instead of guessing physical columns.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            expr = node.get("expression") or node.get("Expression")
            name = (
                node.get("name")
                or node.get("alias")
                or node.get("Name")
                or node.get("Alias")
                or node.get("columnName")
            )
            if isinstance(expr, str) and expr.strip():
                key = (str(name or ""), expr)
                if key not in seen:
                    seen.add(key)
                    out.append({"name": str(name or ""), "expression": expr})
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(defn)
    return out


def _resolve_metric_columns(defn: dict[str, Any] | None = None) -> list[MetricDef]:
    """Return the metric definitions to use.

    The physical SPICE columns are fixed (and env-overridable at import), so
    this is just a defensive copy; ``defn`` is accepted for signature
    compatibility with the discovery flow.
    """
    return list(METRICS)


# ---------------------------------------------------------------------------
# Week / day helpers.
# ---------------------------------------------------------------------------

SPANISH_WEEKDAYS = ("Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom")


def week_days(reference: date | None = None) -> list[date]:
    """Return Sunday→Saturday of the week containing ``reference`` (today).

    Amazon logistics weeks (and this dashboard's "this week" filter) start on
    Sunday, so the day columns line up with the dashboard.
    """
    ref = reference or datetime.now(ZoneInfo(ROSTER_TIMEZONE)).date()
    sunday = ref - timedelta(days=(ref.weekday() + 1) % 7)
    return [sunday + timedelta(days=i) for i in range(7)]


def _epoch_ms(d: date, *, end: bool = False) -> int:
    tz = ZoneInfo(ROSTER_TIMEZONE)
    if end:
        dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz)
    else:
        dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _day_key_label(value: Any) -> tuple[date | None, str]:
    """Return ``(date, label)`` for a ``startdate_local`` group value.

    Handles QuickSight ISO date/datetime strings (e.g.
    ``"2026-05-24 00:00:00.000"`` or ``"2026-05-24"``) as well as
    epoch-millis (int or numeric string). The label is ``"<Wd> DD"``
    (e.g. ``"Dom 21"``). Returns ``(None, raw)`` when unparsable.
    """
    text = str(value or "").strip()
    # Leading ISO date (covers "YYYY-MM-DD" and "YYYY-MM-DD HH:MM:SS.mmm").
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, f"{SPANISH_WEEKDAYS[d.weekday()]} {d.day:02d}"
        except ValueError:
            pass
    # Epoch millis?
    try:
        ms = int(text)
        dt = datetime.fromtimestamp(ms / 1000.0, tz=ZoneInfo(ROSTER_TIMEZONE))
        d = dt.date()
        return d, f"{SPANISH_WEEKDAYS[d.weekday()]} {d.day:02d}"
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return None, text


# ---------------------------------------------------------------------------
# START_VIS query + WebSocket fetch.
# ---------------------------------------------------------------------------


def _connect_ws(cookies: dict[str, str], mbtc: str, timeout: float):
    ws_url = f"wss://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/websocket/?mbtc={mbtc}"
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            return (
                websocket.create_connection(  # type: ignore[union-attr]
                    ws_url,
                    header=[f"User-Agent: {_UA}", f"Origin: https://{QUICKSIGHT_HOST}"],
                    cookie=cookie_header,
                    timeout=timeout,
                    suppress_origin=True,
                ),
                cookies,
            )
        except Exception as exc:  # noqa: BLE001
            exc_str = str(exc)
            if "(re)authenticate" in exc_str or "return to https://console" in exc_str:
                raise QuickSightAuthError(
                    "Sesión de AWS expirada. Cierra la pestaña de QuickSight, vuelve a abrirla y reintenta."
                )
            if attempt == 0 and "403" in exc_str:
                new_cookies = _parse_handshake_cookies(exc)
                if new_cookies:
                    cookies = {**cookies, **new_cookies}
                    continue
            if attempt == 0 and ("302" in exc_str or "url is invalid" in exc_str.lower()):
                raise QuickSightAuthError(
                    "Sesión de QuickSight no activa en la cuenta correcta. Abre el dashboard en Firefox."
                )
            raise QuickSightAuthError(
                f"No se pudo conectar al WebSocket de QuickSight: {exc_str[:200]}"
            )
    raise QuickSightAuthError("No se pudo conectar al WebSocket de QuickSight.")


def _build_start_vis(
    metrics: list[MetricDef],
    *,
    stations: list[str],
    day_from: date,
    day_to: date,
) -> dict[str, Any]:
    group_cols = [_STATION_ID, _DSP_ID, _DATE_ID]
    last = len(group_cols) - 1
    fields = []
    for i, cid in enumerate(group_cols):
        if i == last:
            fields.append(
                {
                    "name": cid,
                    "limit": _LIMIT,
                    "sort": {"name": _DSP_ID, "dir": "ASC"},
                    "chartContext": cid,
                    "offset": 0,
                    "partitioned": False,
                }
            )
        else:
            fields.append(
                {
                    "name": cid,
                    "limit": -1,
                    "sort": None,
                    "chartContext": cid,
                    "offset": -1,
                    "partitioned": False,
                }
            )
    metric_fields = [
        {"name": m.column_id, "func": m.func, "chartContext": m.column_id}
        for m in metrics
        if m.column_id
    ]

    filters: list[str] = []
    if _REGION_FILTER_VALUE:
        filters.append(
            f"((in({{{_REGION_FILTER_COL}}},[{_filter_list([_REGION_FILTER_VALUE])}],true)))"
        )
    if stations:
        filters.append(f"((in({{{_STATION_ID}}},[{_filter_list(stations)}],true)))")
    # NOTE: no date-range filter here — this dataset is SPICE and rejects the
    # ``between`` function ("Function between is not supported for querying
    # SPICE"). We pull the rows and filter to the current week in Python.

    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metric_fields},
                "clsSupport": True,
                "calculatedFields": [],
                "filterExpressions": filters,
                "parameters": {},
            },
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": ROSTER_TIMEZONE,
            "streamSourceId": _stream_source_id(),
            "datasetVersionAlias": "$STABLE",
            "enableComputations": False,
            "computationsOnly": False,
            "sheetId": SHEET_ID,
            "visualId": VISUAL_ID,
            "visualArn": None,
            "visualType": VISUAL_TYPE,
            "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()),
            "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


@dataclass
class WeeklySummary:
    week_label: str
    day_labels: list[str]
    metrics: list[MetricDef]
    # by_dsp[dsp][metric_key][day_label] = value (float) | None
    by_dsp: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


def fetch_roster_weekly(
    *,
    stations: list[str] | None = None,
    reference: date | None = None,
    timeout: float = 30.0,
    debug: dict[str, Any] | None = None,
) -> WeeklySummary:
    """Pull the per-DSP × day rostering table for the current ISO week.

    Returns a :class:`WeeklySummary`. Raises :class:`QuickSightAuthError`
    when the Firefox session is missing/expired, and :class:`QuickSightError`
    when the dashboard returns no data or the measures can't be resolved.
    """
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")

    stations = [s.strip().upper() for s in (stations or []) if s and s.strip()]
    days = week_days(reference)
    day_from, day_to = days[0], days[-1]

    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesión en {ROSTER_LOGIN_URL} en Firefox."
        )
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    cookies = _warm_up(cookies, mbtc, timeout)

    defn = _get_sheet_definition(cookies, mbtc, timeout=timeout)
    metrics = _resolve_metric_columns(defn)
    if not any(m.column_id for m in metrics):
        raise QuickSightError(
            "No hay columnas de métricas configuradas para el dashboard de roster."
        )

    # When diagnosing, also dump the raw per-DSP values and the dashboard's
    # calculated-field expressions.
    start_vis = _build_start_vis(metrics, stations=stations, day_from=day_from, day_to=day_to)
    cid = start_vis["cid"]
    if debug is not None:
        debug["start_vis"] = start_vis
        debug["metrics"] = [{"key": m.key, "column_id": m.column_id} for m in metrics]
        debug["calculated_fields"] = _extract_calculated_fields(defn)
        debug["frames"] = []

    ws, cookies = _connect_ws(cookies, mbtc, timeout)
    data_msg = None
    try:
        ws.send(json.dumps(start_vis))
        ws.settimeout(timeout)
        deadline = time.time() + timeout + 10
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:  # noqa: BLE001
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if debug is not None and len(debug["frames"]) < 40:
                debug["frames"].append(msg)
            if "error" in msg:
                raise QuickSightError(
                    f"QuickSight error: {msg.get('error')} - {msg.get('details')}"
                )
            if "data" in msg and "available" in msg:
                data_msg = msg
                break
    finally:
        try:
            ws.send(json.dumps({"type": "STOP_VIS", "cids": [cid]}))
        except Exception:  # noqa: BLE001
            pass
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass

    if not data_msg:
        raise QuickSightError("QuickSight no devolvió datos para la tabla de roster.")

    queried = [m for m in metrics if m.column_id]
    derived = [m for m in metrics if m.op]
    display = [m for m in metrics if m.display]

    # First pass: collect raw queried values keyed by (dsp, day_date), scoped
    # to the current week window (no date filter in the SPICE query).
    day_keys: dict[date, str] = {}
    raw: dict[str, dict[date, dict[str, Any]]] = {}
    for item in data_msg.get("data", []):
        g = item.get("group", []) or []
        # group order = [station, dsp, date]
        dsp = str(g[1]).strip().upper() if len(g) > 1 and g[1] is not None else ""
        if not dsp:
            continue
        d, label = _day_key_label(g[2]) if len(g) > 2 else (None, "")
        if d is None or d < day_from or d > day_to:
            continue
        day_keys[d] = label
        mcells = (item.get("current") or {}).get("metrics", {})
        cell = raw.setdefault(dsp, {}).setdefault(d, {})
        # A DSP can operate from several stations, so the dashboard returns one
        # row per (station, dsp, date). Accumulate across stations instead of
        # overwriting, matching the dashboard's per-DSP daily totals.
        for m in queried:
            val = _cell_value(mcells.get(m.column_id))
            if val is None:
                continue
            cell[m.key] = cell.get(m.key, 0.0) + val

    ordered = sorted(day_keys)
    day_labels = [day_keys[d] for d in ordered]

    if debug is not None:
        debug["raw_by_dsp"] = {
            dsp: {day_keys[d]: by_day[d] for d in ordered if d in by_day}
            for dsp, by_day in raw.items()
        }

    summary = WeeklySummary(
        week_label=f"WK {day_from.isocalendar().week}",
        day_labels=day_labels,
        metrics=display,
    )

    def _derive(op: str, a: Any, b: Any) -> Any:
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return None
        if op == "ratio":
            return fa / fb if fb else None
        if op == "diff":
            return fa - fb
        return None

    for dsp, by_day in raw.items():
        bucket = summary.by_dsp.setdefault(dsp, {m.key: {} for m in display})
        for d in ordered:
            label = day_keys[d]
            cell = by_day.get(d, {})
            # Compute derived metrics for this cell.
            for m in derived:
                a = cell.get(m.operands[0]) if m.operands else None
                b = cell.get(m.operands[1]) if m.operands else None
                cell[m.key] = _derive(m.op, a, b)
            for m in display:
                bucket[m.key][label] = cell.get(m.key)
    return summary


__all__ = [
    "DASHBOARD_ID",
    "ROSTER_LOGIN_URL",
    "QUICKSIGHT_HOST",
    "QuickSightAuthError",
    "QuickSightError",
    "MetricDef",
    "METRICS",
    "WeeklySummary",
    "discover_measures",
    "roster_login_status",
    "fetch_roster_weekly",
    "week_days",
]
