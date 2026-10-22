"""QuickSight **ROBL Offsets** integration (live extraction).

Feeds the "ROBL Offsets" DO tool without operator downloads: pulls the
PvA ROBL offsets table and the route-level plan-vs-actual rows straight
from the source dashboards, reusing the same Firefox/Midway QuickSight
session machinery as the Phoenix / LMR / scheduling clients.

Sources (ids frozen from a live discovery, 2026-08-29):

* Offsets — dashboard ``4ab325da`` sheet "PvA Offset (Service Type)",
  visual "[EU] ROBL PvA Offsets - Service Type Level". Dataset
  ``5ff4db8e``: every field is a plain GROUP column (no metrics), and a
  server-side ``in(station_code, ...)`` filter works directly.
* Route rows — dashboard ``979028dd`` sheet "UE30 Deep Dive", visual
  "UE30 Bridge". Dataset ``4c14cccd``. HARD-WON LESSONS (verified live):
  - a filtered column MUST also be present in the group fields, or the
    stream fails with a bare ``INTERNAL_ERROR``;
  - ``AVERAGE`` aggregations fail on this dataset (same error) — query
    ``SUM`` and, as the rows are one-per-route, SUM == the row value;
  - metric cells arrive keyed by the lowercase agg name
    (``{"sum": 431.2}`` / ``{"distinct_count": 16}``), so the decoder
    takes the first value whatever the key;
  - ``Full PvA %`` / ``ROBL30 %`` are dashboard calculated fields whose
    expressions are NOT retrievable via the API — they are DERIVED here
    from ``plan_time`` / ``actual_time`` / ``is_route_ue30`` instead
    (ROBL30 per route = 100 when the route ran >= 30 min over plan,
    matching the dashboard's UE30 flag).

The fetchers return :class:`pandas.DataFrame` objects whose column
names mirror the official CSV exports, so the deterministic engine
(``app.tools.do.robl_offsets``) parses live data and operator uploads
through the exact same code path.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import re
import time
import uuid
from typing import Any

import pandas as pd
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

# --- Offsets dashboard ("[EU] ROBL PvA Offsets") ---------------------------
OFFSETS_DASHBOARD_ID = "4ab325da-c75c-4cb3-8a43-3647c60f1fc9"
OFFSETS_SHEET_ID = f"{OFFSETS_DASHBOARD_ID}_16cd168a-fb14-4391-8ac1-12e0e9a371a9"
OFFSETS_VISUAL_ID = f"{OFFSETS_DASHBOARD_ID}_8276edb4-0812-4ed9-8796-bb5442e621a8"
OFFSETS_STREAM_SOURCE_ID = "7fa5cff5-7131-433b-adb4-2eaac2cd2022"
_PO = "5ff4db8e-8506-4533-adbb-09fc6021f397"

# --- ROBL30 root-cause sheet (same offsets dashboard) ----------------------
# Carries the OFFICIAL ROBL30 flag per route ("Not ROBL30" vs a root
# cause), so the per-DSP ROBL30% here matches the program metric — unlike
# the plan-vs-actual UE30 proxy derived from the route rows.
RC_SHEET_ID = f"{OFFSETS_DASHBOARD_ID}_566a1d8b-99a9-4c17-89b6-bf70f664b2ee"
RC_VISUAL_ID = f"{OFFSETS_DASHBOARD_ID}_5f1fe952-4b82-4245-88c3-f4457088bd43"
RC_STREAM_SOURCE_ID = "c09ccfdc-2db2-41cf-b887-777933c21c4d"
_RC = "0de95e82-3b14-4716-99d2-01f66a6ab77d"

# --- RABL tables (ROBL Deep Dive tab, same offsets dashboard) --------------
# Daily RABL% / eROBL30% / ROBL eROBL% / OODT% / UTR eROBL% per DSP — the
# source of the RABL exception TT check. Ids frozen from a live discovery
# (2026-09-01) against the "RABL Deep Dive (Raw Data)" visual. HARD-WON
# LESSONS (verified live):
# - The visual's percentage columns are ANALYSIS calculated fields (bare
#   uuids, no dataset prefix). They are NOT queryable via the API: CUSTOM
#   dies with "should be defined as either an analysis or a data prep
#   calculated field" and MIN/MAX with "field not found".
# - The dataset DOES expose the per-route FLAG columns behind them
#   (``rabl_flag`` / ``erobl_30_flag`` / ``oodt_flag`` /
#   ``robl_caused_erobl`` / ``utr_caused_erobl`` + ``route_count``), so
#   each percentage = 100 * SUM(flag) / SUM(route_count).
# - ``rabl_flag`` counts routes that did NOT achieve block length (the
#   RNA flag): RNA% == eROBL30% + OODT% held exactly on every live row
#   (DCZ4), therefore RABL% = 100 - RNA%.
# - The stream source id is the visual's ``preparedDataSourceId`` (cube
#   client precedent), not a streamSourceId from the layout.
RABL_SHEET_ID = f"{OFFSETS_DASHBOARD_ID}_2a426574-991a-491f-b14c-b6dcf64d5809"
RABL_VISUAL_ID = f"{OFFSETS_DASHBOARD_ID}_bc68648f-7f02-407c-8e5a-9f96d65821e2"
RABL_STREAM_SOURCE_ID = "b4a85c97-fcbb-465f-b3d9-b3cfb2644231"
_RB = "32055285-d101-4d88-9934-037ef4e8fd24"

# --- Route-level dashboard (UE30 Deep Dive) --------------------------------
ROUTES_DASHBOARD_ID = "979028dd-d59f-4a3f-92c6-b0a46fbf993f"
ROUTES_SHEET_ID = f"{ROUTES_DASHBOARD_ID}_ae4080a4-1ea2-42ee-8e0d-caff5bff5411"
ROUTES_VISUAL_ID = f"{ROUTES_DASHBOARD_ID}_dc2f49f2-ae4e-43eb-acc2-26ac2242dd3b"
ROUTES_STREAM_SOURCE_ID = "b8566f96-c2eb-47c1-84ce-9069ff0a7fb6"
_PR = "4c14cccd-cdea-4be7-a2d3-7d3a98937af3"

LOGIN_URL = (
    f"https://{HOST}/sn/account/{ALIAS}/dashboards/{OFFSETS_DASHBOARD_ID}"
)

# Offsets: how far back to keep rows (the engine needs WK-1/WK0/WK+1; the
# dataset carries months). Future rows (WK+1 plan) are always kept.
_OFFSETS_LOOKBACK_DAYS = 16
# Route rows: WK0 + WK-1 window (the stream caps at ~2048 rows anyway).
_ROUTES_LOOKBACK_DAYS = 16


def _page_url(dashboard_id: str) -> str:
    return f"https://{HOST}/sn/account/{ALIAS}/dashboards/{dashboard_id}"


def _fetch_mbtc(dashboard_id: str, cookies: dict[str, str], timeout: float = 20.0) -> str:
    try:
        resp = requests.get(
            _page_url(dashboard_id),
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies, timeout=timeout, allow_redirects=False,
        )
        if resp.status_code != 200:
            return ""
        m = re.search(r'"csrf"\s*:\s*"([^"]+)"', _html.unescape(resp.text))
        return m.group(1) if m else ""
    except requests.RequestException:
        return ""


def _warm_up(
    dashboard_id: str, sheet_id: str, cookies: dict[str, str], mbtc: str, timeout: float
) -> dict[str, str]:
    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{dashboard_id}/sheets/{sheet_id}"
        f"?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA, "Accept": "*/*", "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc, "x-amzn-target": "api",
        "Referer": _page_url(dashboard_id),
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


def _open_session(
    dashboard_id: str, sheet_id: str, timeout: float = 30.0
) -> tuple[dict[str, str], str]:
    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{HOST}/ en Firefox."
        )
    # The dashboard page intermittently serves the JS bootstrap stub (no
    # csrf) even with a live session — Firefox rotates the session cookie
    # while we read its DB. A couple of re-reads recovers (verified live
    # 2026-09-01: attempt 1 stub, attempt 2 OK).
    mbtc = ""
    for attempt in range(3):
        mbtc = _fetch_mbtc(dashboard_id, cookies, timeout=timeout)
        if mbtc:
            break
        time.sleep(2.0)
        cookies = _read_cookies() or cookies
    if not mbtc:
        raise QuickSightAuthError(
            "No se pudo leer el token CSRF de QuickSight (sesion expirada)."
        )
    cookies = _warm_up(dashboard_id, sheet_id, cookies, mbtc, timeout)
    return cookies, mbtc


def _filter_in(col_id: str, values: list[str]) -> str:
    quoted = ",".join(f'"{v}"' for v in values)
    return f"((in({{{col_id}}},[{quoted}],true)))"


def _build_start_vis(
    *,
    dashboard_id: str,
    sheet_id: str,
    visual_id: str,
    stream_source_id: str,
    group_cols: list[str],
    metric_cols: list[tuple[str, str]],
    filters: list[str],
    sort_col: str,
    sort_dir: str = "DESC",
    limit: int = 50000,
    vtype: str = "PIVOT_TABLE",
) -> dict[str, Any]:
    last = len(group_cols) - 1
    fields = []
    for i, cid in enumerate(group_cols):
        if i == last:
            fields.append({"name": cid, "limit": limit,
                           "sort": {"name": sort_col, "dir": sort_dir},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    metrics = [{"name": cid, "func": func, "chartContext": cid} for cid, func in metric_cols]
    metrics.append({"name": "!calc.count.0.0", "func": "CUSTOM"})
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": dashboard_id,
            "cfg": {"excludeCountStar": True,
                    "group": {"fields": fields, "metrics": metrics},
                    "clsSupport": True,
                    "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                    "filterExpressions": filters, "parameters": {}},
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": stream_source_id, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": sheet_id, "visualId": visual_id, "visualArn": None,
            "visualType": vtype, "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _stream_vis(
    start_vis: dict[str, Any], cookies: dict[str, str], mbtc: str, timeout: float
) -> dict[str, Any]:
    """First data frame of the visual (``NO_DATA_FOUND`` → empty frame)."""
    ws, _ = _connect_ws(cookies, mbtc, timeout)
    cid = start_vis["cid"]
    data_msg: dict[str, Any] | None = None
    error_text = ""
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
            if "error" in msg:
                error_text = f"{msg.get('error')} - {str(msg.get('internalMessage'))[:200]}"
                break
            if msg.get("status") == "NO_DATA_FOUND":
                data_msg = {"data": []}
                break
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
    if error_text:
        raise QuickSightError(f"QuickSight error: {error_text}")
    if data_msg is None:
        raise QuickSightError("QuickSight no devolvio datos (timeout del stream).")
    return data_msg


def _cell_value(cell: Any) -> Any:
    """Metric cell → value, whatever the agg key (sum / distinct_count...)."""
    if not isinstance(cell, dict):
        return cell
    for value in cell.values():
        return value
    return None


def _rows_from_frame(
    data_msg: dict[str, Any],
    group_names: list[str],
    metric_pairs: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        group = item.get("group", [])
        cells = (item.get("current") or {}).get("metrics", {})
        record: dict[str, Any] = {}
        for idx, name in enumerate(group_names):
            record[name] = group[idx] if idx < len(group) else None
        for cid, name in metric_pairs:
            record[name] = _cell_value(cells.get(cid))
        rows.append(record)
    return rows


def robl_login_status() -> dict[str, Any]:
    """Whether the Firefox session can auto-fetch the offsets dashboard.

    One light probe against the offsets dashboard covers both sources:
    they live in the same QuickSight account/session.
    """
    return dashboard_login_status(
        dashboard_id=OFFSETS_DASHBOARD_ID,
        sheet_id=OFFSETS_SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(OFFSETS_DASHBOARD_ID),
    )


def fetch_offsets_table(station: str, *, timeout: float = 45.0) -> pd.DataFrame:
    """PvA ROBL offsets rows for ``station`` → export-shaped DataFrame.

    Columns mirror the dashboard CSV export so ``engine.parse_offsets``
    consumes live data unchanged. Rows are trimmed to the analysis
    window (last ~2 weeks + every future/planned day).
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para el fetch de offsets.")
    cookies, mbtc = _open_session(OFFSETS_DASHBOARD_ID, OFFSETS_SHEET_ID, timeout)

    groups = [
        (f"{_PO}.ofd_date", "OFD"),
        (f"{_PO}.station_code", "DS"),
        (f"{_PO}.company_code", "DSP"),
        (f"{_PO}.wave", "Wave"),
        (f"{_PO}.service_type", "Service Type"),
        (f"{_PO}.modified_by", "Modified By"),
        (f"{_PO}.reason_change", "reason_change"),
        (f"{_PO}.last_modified", "Last Modified"),
        (f"{_PO}.pva_robl_offset", "pva_robl_offset"),
        (f"{_PO}.final_pva_robl_offset", "final_pva_robl_offset"),
        (f"{_PO}.total_reduction", "total_reduction"),
        (f"{_PO}.paid_minutes", "paid_minutes"),
    ]
    start_vis = _build_start_vis(
        dashboard_id=OFFSETS_DASHBOARD_ID,
        sheet_id=OFFSETS_SHEET_ID,
        visual_id=OFFSETS_VISUAL_ID,
        stream_source_id=OFFSETS_STREAM_SOURCE_ID,
        group_cols=[cid for cid, _ in groups],
        metric_cols=[],
        filters=[_filter_in(f"{_PO}.station_code", [station])],
        sort_col=f"{_PO}.ofd_date",
        sort_dir="DESC",
        vtype="TABLE",
    )
    data = _stream_vis(start_vis, cookies, mbtc, timeout)
    rows = _rows_from_frame(data, [name for _, name in groups], [])

    cutoff = _dt.date.today() - _dt.timedelta(days=_OFFSETS_LOOKBACK_DAYS)
    kept: list[dict[str, Any]] = []
    for row in rows:
        stamp = str(row.get("OFD") or "")[:10]
        try:
            day = _dt.date.fromisoformat(stamp)
        except ValueError:
            continue
        if day >= cutoff:
            row["OFD"] = stamp
            kept.append(row)
    return pd.DataFrame(kept)


def fetch_metrics_table(station: str, *, timeout: float = 45.0) -> pd.DataFrame:
    """Route-level plan-vs-actual rows for ``station`` (STANDARD + rest).

    The engine re-filters to STANDARD PARCEL itself; sending every
    service type keeps the excluded-count note meaningful. ``Full PvA %``
    and ``ROBL30 %`` are derived (see module docstring) because the
    dashboard versions are calculated fields the API cannot evaluate.
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para el fetch de rutas.")
    cookies, mbtc = _open_session(ROUTES_DASHBOARD_ID, ROUTES_SHEET_ID, timeout)

    # LESSON: station_code must be in the group fields for the filter to
    # work on this dataset (a filter-only reference dies with
    # INTERNAL_ERROR).
    groups = [
        (f"{_PR}.station_code", "Station"),
        (f"{_PR}.date", "Date"),
        (f"{_PR}.route_id", "Route Code"),
        (f"{_PR}.plan_service_type_name", "Plan Service Type"),
        (f"{_PR}.company_code", "Company Code"),
        (f"{_PR}.is_route_ue30", "_is_ue30"),
        (f"{_PR}.ue30_root_cause", "U30 Root Cause"),
    ]
    # LESSON: AVERAGE breaks on this dataset — SUM only (rows are
    # one-per-route, so SUM equals the row value).
    metrics = [
        (f"{_PR}.plan_time", "SUM", "Plan Minutes"),
        (f"{_PR}.actual_time", "SUM", "Actual Time Adjusted"),
        (f"{_PR}.paid_block_length_minutes", "SUM", "Paid Minutes"),
        (f"{_PR}.paid_block_length_on_road_minutes", "SUM", "Paid On Road Minutes"),
        (f"{_PR}.redel_time_actual", "SUM", "Redelivery Minutes"),
        (f"{_PR}.unattempted_time_plan", "SUM", "Unattempted Minutes"),
        (f"{_PR}.main_driver_affinity", "SUM", "Main Driver Affinity"),
        (f"{_PR}.route_adherence", "SUM", "Route Adherence"),
    ]
    start_vis = _build_start_vis(
        dashboard_id=ROUTES_DASHBOARD_ID,
        sheet_id=ROUTES_SHEET_ID,
        visual_id=ROUTES_VISUAL_ID,
        stream_source_id=ROUTES_STREAM_SOURCE_ID,
        group_cols=[cid for cid, _ in groups],
        metric_cols=[(cid, func) for cid, func, _ in metrics],
        filters=[_filter_in(f"{_PR}.station_code", [station])],
        sort_col=f"{_PR}.date",
        sort_dir="DESC",
    )
    data = _stream_vis(start_vis, cookies, mbtc, timeout)
    rows = _rows_from_frame(
        data,
        [name for _, name in groups],
        [(cid, name) for cid, _, name in metrics],
    )

    cutoff = _dt.date.today() - _dt.timedelta(days=_ROUTES_LOOKBACK_DAYS)
    kept: list[dict[str, Any]] = []
    for row in rows:
        stamp = str(row.get("Date") or "")[:10]
        try:
            day = _dt.date.fromisoformat(stamp)
        except ValueError:
            continue
        if day < cutoff or day > _dt.date.today():
            continue
        row["Date"] = stamp

        plan = _as_float(row.get("Plan Minutes"))
        actual = _as_float(row.get("Actual Time Adjusted"))
        full_pva = None
        if plan and actual is not None and plan > 0:
            full_pva = round(100.0 * (plan - actual) / plan, 1)
        row["Full PvA %"] = full_pva

        # Route-level ROBL30: the dashboard's UE30 flag (actual >= plan
        # + 30 min). 100/0 so the DSP weighted mean = % of routes over.
        ue30 = row.pop("_is_ue30", None)
        row["ROBL30 %"] = 100.0 if ue30 == 1 else 0.0

        # Affinity/adherence arrive as 0-1 fractions; the engine's
        # percent auto-scale handles them, but normalize here so the
        # export and live paths look identical.
        for col in ("Main Driver Affinity", "Route Adherence"):
            value = _as_float(row.get(col))
            if value is not None and abs(value) <= 1.5:
                row[col] = round(value * 100.0, 1)
        kept.append(row)
    return pd.DataFrame(kept)


# RABL rows: the audited window is at most WK-1 + WK0.
_RABL_LOOKBACK_DAYS = 16

_RABL_GROUPS: list[tuple[str, str]] = [
    (f"{_RB}.date", "date"),
    (f"{_RB}.station_code", "station"),
    (f"{_RB}.company_code", "dsp"),
    (f"{_RB}.service_type_group", "service_group"),
]
# Per-route flag columns behind the pivot's percentages. ``rabl_flag``
# counts routes that MISSED block length (RNA), verified live: on every
# DCZ4 row, SUM(rabl_flag) == SUM(erobl_30_flag) + SUM(oodt_flag).
_RABL_FLAGS: list[tuple[str, str]] = [
    (f"{_RB}.rabl_flag", "rna"),
    (f"{_RB}.erobl_30_flag", "erobl30"),
    (f"{_RB}.oodt_flag", "oodt"),
    (f"{_RB}.robl_caused_erobl", "robl_erobl"),
    (f"{_RB}.utr_caused_erobl", "utr_erobl"),
    (f"{_RB}.route_count", "routes"),
]


def fetch_rabl_daily(station: str, *, timeout: float = 45.0) -> list[dict[str, Any]]:
    """Daily RABL-table percentages per DSP for ``station`` (ND STANDARD).

    Returns ``[{"date", "dsp", "rabl", "erobl30", "robl_erobl", "oodt",
    "utr_erobl"}, ...]`` — the shape ``engine.analyze_rabl`` consumes.
    Each percentage = 100 * SUM(flag) / SUM(route_count) over the
    STANDARD service-type group (the RABL exception check audits the ND
    standard metric; NURSERY / OTHER rows are dropped), and
    ``RABL% = 100 - RNA%``.
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para el fetch de RABL.")
    cookies, mbtc = _open_session(OFFSETS_DASHBOARD_ID, RABL_SHEET_ID, timeout)

    start_vis = _build_start_vis(
        dashboard_id=OFFSETS_DASHBOARD_ID,
        sheet_id=RABL_SHEET_ID,
        visual_id=RABL_VISUAL_ID,
        stream_source_id=RABL_STREAM_SOURCE_ID,
        group_cols=[cid for cid, _ in _RABL_GROUPS],
        metric_cols=[(cid, "SUM") for cid, _ in _RABL_FLAGS],
        filters=[_filter_in(f"{_RB}.station_code", [station])],
        sort_col=f"{_RB}.date",
        sort_dir="DESC",
        vtype="TABLE",
    )
    data = _stream_vis(start_vis, cookies, mbtc, timeout)
    raw = _rows_from_frame(
        data,
        [name for _, name in _RABL_GROUPS],
        [(cid, name) for cid, name in _RABL_FLAGS],
    )

    cutoff = _dt.date.today() - _dt.timedelta(days=_RABL_LOOKBACK_DAYS)
    rows: list[dict[str, Any]] = []
    for item in raw:
        if str(item.get("service_group") or "").strip().upper() != "STANDARD":
            continue
        stamp = str(item.get("date") or "")[:10]
        try:
            day = _dt.date.fromisoformat(stamp)
        except ValueError:
            continue
        if day < cutoff or day > _dt.date.today():
            continue
        st = str(item.get("station") or "").strip().upper()
        if st and st != station:
            continue
        dsp = str(item.get("dsp") or "").split(" - ", 1)[0].strip().upper()
        if not dsp:
            continue
        routes = _as_float(item.get("routes"))
        if not routes:
            continue

        def _pct(key: str) -> float:
            return round(100.0 * (_as_float(item.get(key)) or 0.0) / routes, 1)

        rows.append(
            {
                "date": stamp,
                "dsp": dsp,
                "rabl": round(100.0 - _pct("rna"), 1),
                "erobl30": _pct("erobl30"),
                "robl_erobl": _pct("robl_erobl"),
                "oodt": _pct("oodt"),
                "utr_erobl": _pct("utr_erobl"),
            }
        )
    return rows


def fetch_official_robl30(station: str, *, timeout: float = 45.0) -> dict[str, dict[str, dict[str, Any]]]:
    """OFFICIAL ROBL30% per week and DSP from the root-cause sheet.

    Returns ``{"WK35": {"CDOR": {"robl30": 12.9, "robl30_routes": 21,
    "total_routes": 163}, ...}, ...}``. The dataset flags every route as
    either "Not ROBL30" or a root cause, so
    ``robl30% = routes_with_cause / total`` reproduces the dashboard
    metric (the route-level dataset only offers the plan-vs-actual UE30
    proxy, which over-flags vs the paid-block-based program number).

    NOTE: this dataset only exposes ``company_name`` ("CDOR - Corredor
    ..."), not ``company_code`` — the DSP code is the prefix.
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para el fetch de ROBL30.")
    cookies, mbtc = _open_session(OFFSETS_DASHBOARD_ID, RC_SHEET_ID, timeout)

    groups = [
        (f"{_RC}.station_code", "station"),
        (f"{_RC}.year_week", "year_week"),
        (f"{_RC}.company_name", "company"),
        (f"{_RC}.robl30_root_cause", "cause"),
    ]
    start_vis = _build_start_vis(
        dashboard_id=OFFSETS_DASHBOARD_ID,
        sheet_id=RC_SHEET_ID,
        visual_id=RC_VISUAL_ID,
        stream_source_id=RC_STREAM_SOURCE_ID,
        group_cols=[cid for cid, _ in groups],
        metric_cols=[(f"{_RC}.route_id", "DISTINCT_COUNT")],
        filters=[_filter_in(f"{_RC}.station_code", [station])],
        sort_col=f"{_RC}.year_week",
        sort_dir="DESC",
    )
    data = _stream_vis(start_vis, cookies, mbtc, timeout)
    rows = _rows_from_frame(
        data, [name for _, name in groups], [(f"{_RC}.route_id", "routes")]
    )

    tally: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        year_week = str(row.get("year_week") or "")
        m = re.match(r"^(\d{4})-(\d{1,2})$", year_week)
        if not m:
            continue
        week_label = f"WK{int(m.group(2)):02d}"
        company = str(row.get("company") or "").strip()
        dsp = company.split(" - ", 1)[0].strip().upper() or company.upper()
        if not dsp:
            continue
        routes = row.get("routes")
        try:
            count = float(routes or 0)
        except (TypeError, ValueError):
            count = 0.0
        slot = tally.setdefault(week_label, {}).setdefault(
            dsp, {"over": 0.0, "total": 0.0}
        )
        slot["total"] += count
        if str(row.get("cause") or "").strip().lower() != "not robl30":
            slot["over"] += count

    out: dict[str, dict[str, dict[str, Any]]] = {}
    for week_label, dsps in tally.items():
        out[week_label] = {}
        for dsp, slot in dsps.items():
            total = slot["total"]
            out[week_label][dsp] = {
                "robl30": round(100.0 * slot["over"] / total, 1) if total else None,
                "robl30_routes": int(slot["over"]),
                "total_routes": int(total),
            }
    return out


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "LOGIN_URL",
    "OFFSETS_DASHBOARD_ID",
    "ROUTES_DASHBOARD_ID",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_metrics_table",
    "fetch_offsets_table",
    "fetch_official_robl30",
    "fetch_rabl_daily",
    "robl_login_status",
]
