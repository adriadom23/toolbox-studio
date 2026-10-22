"""QuickSight **SCAS Risk Summary** integration (Labor / Cube-delta risk flag).

Third QuickSight source for the Labor Calculator. Reads the **Risk Summary**
tab of the SCAS "ORBIT" dashboard — the *same* dashboard (``0667888e-...``) and
QuickSight session as the Cube / OV% client, just a different sheet
(``e66c46a7-...`` = "Risk Summary").

Background (reverse-engineered live against the dashboard):

* The Risk Summary tab shows, per station, a set of green-check / red-cross
  flags rendered with QuickSight conditional-formatting icon sets
  (``cf_checkmark`` #2CAD00 = ok, ``cf_x`` #DE3B00 = risk).
* The dashboard's own legend documents the **Labor Flag** as:
  *"Stations with Cube delta% > 5%"*.
* The visual's ``cube delta%`` is reproduced from its physical measures as
  ``SUM(cube delta) / SUM(cube ov)``. Values above 5% are red; 5% or below
  are green.

The sheet / visual / stream ids remain overridable so the integration can be
re-pinned if the dashboard moves. The Labor formula and threshold deliberately
are not configurable: they mirror the published business rule.

Env overrides:
    QS_RISK_DASHBOARD_ID, QS_RISK_SHEET_ID, QS_RISK_VISUAL_ID,
    QS_RISK_STREAM_ID
"""
from __future__ import annotations

import html as _html
import os
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
    dashboard_login_status,
)

# --- Dashboard / sheet / visual ids (pinned live; overridable via env) -------
DASHBOARD_ID = (
    os.getenv("QS_RISK_DASHBOARD_ID", "").strip()
    or "0667888e-0923-4c54-bf90-535794db34ef"
)
SHEET_ID = (
    os.getenv("QS_RISK_SHEET_ID", "").strip()
    or f"{DASHBOARD_ID}_e66c46a7-af75-4136-9b0b-73cfba768084"  # "Risk Summary"
)
VISUAL_ID = (
    os.getenv("QS_RISK_VISUAL_ID", "").strip()
    or f"{DASHBOARD_ID}_fdb4c508-7ca2-495d-8337-adccad74fc4a"  # per-station pivot
)
STREAM_SOURCE_ID = (
    os.getenv("QS_RISK_STREAM_ID", "").strip() or "a4b28310-986e-424a-a31d-14f548e72db0"
)

# Dataset column namespace for the Risk Summary pivot.
_NS = os.getenv("QS_RISK_DATASET_NS", "").strip() or "9202748b-7b74-4867-89cd-a2b8c56c7da9"
STATION_COL = f"{_NS}.station"
EXEC_COL = f"{_NS}.execution date"
CUBE_DELTA_COL = f"{_NS}.cube delta"
CUBE_OV_COL = f"{_NS}.cube ov"
CUBE_OV_SNOP_COL = f"{_NS}.cube ov snop"
CUBE_WEEKLY_COL = f"{_NS}.cube_weekly"
CUBE_ACTUALS_COL = f"{_NS}.cube_actuals"

_LABOR_THRESHOLD = 0.05

LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"

# Keyword matchers kept only for the /labor-risk/debug diagnostic.
_LABOR_KEYS = [k.strip().lower() for k in (os.getenv("QS_RISK_LABOR_KEYS", "") or "labor,labour,cube delta").split(",") if k.strip()]
_STATION_KEYS = ["station", "delivery_station", "ds_code", "warehouse", "site"]
_VALUE_AXES = {"VALUE", "VALUES", "METRIC", "METRICS", "MEASURE", "MEASURES", "SIZE", "COLOR"}


# --- Session helpers (mirror the Cube / OV% client) --------------------------
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


def _sheet_url(mbtc: str) -> str:
    return (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"
        f"?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )


def _sheet_headers(mbtc: str) -> dict[str, str]:
    return {
        "User-Agent": _UA, "Accept": "*/*", "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc, "x-amzn-target": "api", "Referer": _page_url(),
    }


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    merged = dict(cookies)
    try:
        resp = requests.get(
            _sheet_url(mbtc), headers=_sheet_headers(mbtc), cookies=merged,
            timeout=timeout, allow_redirects=False,
        )
        merged.update(_extract_set_cookies(resp))
        if resp.status_code in (401, 403):
            resp2 = requests.get(
                _sheet_url(mbtc), headers=_sheet_headers(mbtc), cookies=merged,
                timeout=timeout, allow_redirects=False,
            )
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


def _fetch_sheet_json(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, Any] | None:
    try:
        resp = requests.get(
            _sheet_url(mbtc), headers=_sheet_headers(mbtc), cookies=cookies,
            timeout=timeout, allow_redirects=False,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def risk_login_status() -> dict[str, Any]:
    """Whether the QuickSight session can reach the SCAS Risk Summary sheet."""
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


# --- Data query --------------------------------------------------------------
def _build_start_vis() -> dict[str, Any]:
    """Group by (station, execution date), SUM the cube measures.

    The Risk Summary pivot is a SPICE table; its physical measures only
    aggregate with SUM (MIN/MAX/AVG return null). Grouping per station and
    execution date lets us reproduce the visual's Labor ratio for D+1."""
    group = [STATION_COL, EXEC_COL]
    last = len(group) - 1
    fields = []
    for i, cid in enumerate(group):
        if i == last:
            fields.append({"name": cid, "limit": 50000,
                           "sort": {"name": EXEC_COL, "dir": "DESC"},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    measure_cols = [
        CUBE_DELTA_COL,
        CUBE_OV_COL,
        CUBE_OV_SNOP_COL,
        CUBE_WEEKLY_COL,
        CUBE_ACTUALS_COL,
    ]
    seen: set[str] = set()
    metrics = []
    for cid in measure_cols:
        if cid in seen:
            continue
        seen.add(cid)
        metrics.append({"name": cid, "func": "SUM", "chartContext": cid})
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
            "streamSourceId": STREAM_SOURCE_ID, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": SHEET_ID, "visualId": VISUAL_ID, "visualArn": None,
            "visualType": "PIVOT_TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _rows_for_station(data: dict[str, Any], station: str) -> dict[str, dict[str, float]]:
    """Map ``execution_date[:10] -> {measure: value}`` for ``station``."""
    out: dict[str, dict[str, float]] = {}
    for item in data.get("data", []):
        g = item.get("group", [])
        st = str(g[0]).strip().upper() if g else ""
        if st != station or len(g) < 2:
            continue
        m = (item.get("current") or {}).get("metrics", {})
        out[str(g[1])[:10]] = {
            "cube_delta": _metric_value(m.get(CUBE_DELTA_COL)),
            "cube_ov": _metric_value(m.get(CUBE_OV_COL)),
            "cube_ov_snop": _metric_value(m.get(CUBE_OV_SNOP_COL)),
            "cube_weekly": _metric_value(m.get(CUBE_WEEKLY_COL)),
            "cube_actuals": _metric_value(m.get(CUBE_ACTUALS_COL)),
        }
    return out


def _pick_row(rows: dict[str, dict[str, float]], date: str | None) -> tuple[str, dict[str, float]] | None:
    """Pick the forecast row for the plan date, else the latest one with data."""
    if not rows:
        return None
    target = (date or "").strip()[:10] or None
    if target and target in rows:
        return target, rows[target]
    # Latest execution date that carries both sides of the Labor comparison.
    dated = sorted(rows.items(), reverse=True)
    for d, vals in dated:
        if vals.get("cube_delta") is not None and vals.get("cube_ov"):
            return d, vals
    return dated[0]


def _labor_flag(cube_delta: float | None, cube_ov: float | None) -> tuple[float | None, bool | None]:
    """Return the official ``(cube_delta_pct, ok)`` Labor result.

    Risk Summary defines Cube delta% as Cube delta divided by Cube OV. A
    delta strictly above 5% is red; exactly 5% or below is green.
    """
    if cube_delta is None or not cube_ov:
        return None, None
    pct = round(float(cube_delta) / float(cube_ov), 12)
    return pct, not (pct > _LABOR_THRESHOLD)


def fetch_labor_status(
    *, station: str, date: str | None = None, timeout: float = 35.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
) -> dict[str, Any]:
    """Return the SCAS Risk Summary "Labor" flag for ``station``.

    Response::

        {"cube_delta": <raw sum>, "cube_ov": <comparison denominator>,
         "cube_ov_snop": <diagnostic SnOP cube sum>,
         "cube_delta_pct": <cube_delta / cube_ov>,
         "exec_date": "YYYY-MM-DD", "ok": true|false|None, "labor": "GREEN|RED"}

    ``ok`` is ``True`` (green check) when Cube delta% is at most 5%,
    ``False`` (red cross) when it is more than 5%, and ``None`` when the
    station has no comparable forecast row."""
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta 'station'.")
    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    data = _stream_vis(_build_start_vis(), cookies, mbtc, timeout)
    if not data:
        raise QuickSightError("QuickSight no devolvio datos de Risk Summary.")

    rows = _rows_for_station(data, station)
    picked = _pick_row(rows, date)
    if not picked:
        return {"cube_delta": None, "cube_ov": None, "cube_ov_snop": None,
                "cube_delta_pct": None, "exec_date": None, "ok": None, "labor": None}
    exec_date, vals = picked
    pct, ok = _labor_flag(vals.get("cube_delta"), vals.get("cube_ov"))
    return {
        "cube_delta": vals.get("cube_delta"),
        "cube_ov": vals.get("cube_ov"),
        "cube_ov_snop": vals.get("cube_ov_snop"),
        "cube_delta_pct": pct,
        "exec_date": exec_date,
        "ok": ok,
        "labor": None if ok is None else ("GREEN" if ok else "RED"),
    }


def _discover_visuals(sheet_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Lightweight visual/axis dump for the /labor-risk/debug diagnostic."""
    visuals: dict[str, dict[str, Any]] = {}

    def entry(vid: str) -> dict[str, Any]:
        v = visuals.get(vid)
        if v is None:
            v = {"visual_id": vid, "type": "", "group": [], "values": []}
            visuals[vid] = v
        return v

    def add_axis(v: dict[str, Any], ax: dict[str, Any]) -> None:
        cid = ax.get("columnId") or ax.get("id")
        if not cid:
            return
        axis = str(ax.get("axisName") or ax.get("axis") or "").upper()
        label = ax.get("customLabel") or ax.get("columnName") or ax.get("name") or ""
        col = {"id": cid, "label": label}
        bucket = v["values"] if axis in _VALUE_AXES else v["group"]
        if not any(c["id"] == cid for c in bucket):
            bucket.append(col)

    def walk(node: Any, cur: str | None = None) -> None:
        if isinstance(node, dict):
            vid = node.get("visualId")
            if isinstance(vid, str):
                cur = vid
                v = entry(vid)
                vt = str(node.get("visualType") or node.get("type") or "").upper()
                if vt and not v["type"]:
                    v["type"] = vt
            if cur is not None:
                v = entry(cur)
                for key in ("appliedAxes", "axes", "fieldWells", "columns"):
                    seq = node.get(key)
                    if isinstance(seq, list):
                        for ax in seq:
                            if isinstance(ax, dict):
                                add_axis(v, ax)
            for val in node.values():
                walk(val, cur)
        elif isinstance(node, list):
            for it in node:
                walk(it, cur)

    walk(sheet_json)
    return list(visuals.values())


def debug_risk(*, station: str | None = None, timeout: float = 35.0) -> dict[str, Any]:
    """Diagnostic: config, discovered visuals, and (optionally) a live sample."""
    cookies, mbtc = _open_session(timeout)
    sheet = _fetch_sheet_json(cookies, mbtc, timeout)
    visuals = _discover_visuals(sheet) if sheet else []
    sample = None
    if station:
        try:
            sample = fetch_labor_status(station=station, cookies=cookies, mbtc=mbtc, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            sample = {"error": str(exc)}
    return {
        "dashboard_id": DASHBOARD_ID,
        "sheet_id": SHEET_ID,
        "visual_id": VISUAL_ID,
        "stream_source_id": STREAM_SOURCE_ID,
        "cube_delta_col": CUBE_DELTA_COL,
        "cube_ov_col": CUBE_OV_COL,
        "cube_ov_snop_col": CUBE_OV_SNOP_COL,
        "threshold": _LABOR_THRESHOLD,
        "labor_rule": "cube delta / cube ov > 5%",
        "sample": sample,
        "visuals": visuals,
    }


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "debug_risk",
    "fetch_labor_status",
    "risk_login_status",
]
