"""QuickSight **Cube / OV%** integration (D-1 CPP & D-1 OV% dashboard).

Second QuickSight source for the Labor Calculator. Pulls two D-1 metrics from
the pivot visual of dashboard ``0667888e-...`` (same QuickSight account/session
as the forecast + scheduling clients):

* **D-1 CPP**  -> shown on the ``Cube`` card.
* **D-1 OV%**  -> shown on the ``OV%`` card.

The exact metric column IDs are not hardcoded: they are discovered at query
time from ``GetSheetForDashboard`` (the pivot's ``appliedAxes``) and matched by
label / physical column name. The dashboard / sheet / visual IDs (captured from
the dashboard HAR) and the column-match keywords are overridable via env vars so
the integration can be pinned without a code change.

Env overrides:
    QS_CUBE_DASHBOARD_ID, QS_CUBE_SHEET_ID, QS_CUBE_VISUAL_ID,
    QS_CUBE_STREAM_SOURCE_ID, QS_CUBE_CPP_KEYS, QS_CUBE_OV_KEYS
"""
from __future__ import annotations

import html as _html
import json
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

# IDs captured from the dashboard HAR (overridable via env).
DASHBOARD_ID = os.getenv("QS_CUBE_DASHBOARD_ID", "").strip() or "0667888e-0923-4c54-bf90-535794db34ef"
SHEET_ID = (
    os.getenv("QS_CUBE_SHEET_ID", "").strip()
    or f"{DASHBOARD_ID}_cd3ed1f5-2eb7-4f28-99ca-50f7c97e68ec"
)
VISUAL_ID = (
    os.getenv("QS_CUBE_VISUAL_ID", "").strip()
    or f"{DASHBOARD_ID}_c5f8c431-5c93-40fd-ad92-833deaada029"
)
# Stream source (prepared data source) of the pivot visual — resolved from the
# dashboard metadata; overridable via env.
STREAM_SOURCE_ID = os.getenv("QS_CUBE_STREAM_SOURCE_ID", "").strip() or "49318dbe-75b1-4aae-aec2-81c9317d53eb"

# Dataset column namespace (physical columns of the Cube/OV% dataset).
_DS = os.getenv("QS_CUBE_DATASET_ID", "").strip() or "b24fd360-6a04-4c5f-bb52-7f3b5887a190"


def _phys(col_or_id: str) -> str:
    """Prefix a bare physical column name with the dataset id."""
    c = (col_or_id or "").strip()
    if not c:
        return ""
    return c if "." in c or "-" in c.split(".")[0] else f"{_DS}.{c}"


# The visual's "D-1 CPP" / "D-1 OV%" are calculated fields the query engine
# does NOT accept. They are reproduced from PHYSICAL columns (verified against
# the dashboard for DCZ4 / 2026-07-11):
#   D-1 CPP  = SUM(cube_daily_num) / SUM(cube_metrics_den)   -> 0.2711 (Cube)
#   D-1 OV%  = SUM(avg_ov_share) / OV_DIVISOR                -> 0.0703 (OV%)
# ``avg_ov_share`` only supports SUM aggregation (MIN/MAX/AVG return null) and
# the raw value repeats across a hidden dimension, so the per-station SUM comes
# back multiplied. The dashboard value is that SUM divided by OV_DIVISOR (4 for
# DCZ4). Overridable via env if a station needs a different factor.
CPP_NUM_COL = _phys(os.getenv("QS_CUBE_CPP_NUM", "") or "cube_daily_num")
CPP_DEN_COL = _phys(os.getenv("QS_CUBE_CPP_DEN", "") or "cube_metrics_den")
OV_SHARE_COL = _phys(os.getenv("QS_CUBE_OV_SHARE", "") or "avg_ov_share")
try:
    OV_DIVISOR = float(os.getenv("QS_CUBE_OV_DIVISOR", "") or "4") or 1.0
except ValueError:
    OV_DIVISOR = 4.0

# Row grouping: one row per station (aggregating everything below it). Latest
# execution_date wins.
GROUP_COLS = [f"{_DS}.execution_date", f"{_DS}.region", f"{_DS}.country", f"{_DS}.station"]
_STATION_IDX = 3  # station column within the group tuple

LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"

# Keyword matchers kept only for the /cube/debug diagnostic.
_CPP_KEYS = [k.strip().lower() for k in (os.getenv("QS_CUBE_CPP_KEYS", "") or "cpp,cube,cost per").split(",") if k.strip()]
_OV_KEYS = [k.strip().lower() for k in (os.getenv("QS_CUBE_OV_KEYS", "") or "ov%,ov ,over %,overallocation,ov_pct,ovpercent").split(",") if k.strip()]
_STATION_KEYS = ["station", "delivery_station", "ds_code", "warehouse"]


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


# Axis names that carry metric (measure) columns in a pivot/table visual.
_VALUE_AXES = {"VALUE", "VALUES", "METRIC", "METRICS", "MEASURE", "MEASURES", "SIZE", "COLOR"}


def _discover_visual(sheet_json: dict[str, Any], visual_id: str | None = None) -> dict[str, Any]:
    """Return ``{group, values, stream, all}`` for the pivot visual.

    * ``group``: dimension column ids (ROWS/COLUMNS/CATEGORY axes).
    * ``values``: list of ``{"id","label","name"}`` for VALUE-axis metrics.
    * ``stream``: the visual's prepared data source id (stream source).
    * ``all``: every axis discovered ``{"axis","id","label","name"}`` (diag).
    """
    target = visual_id or VISUAL_ID
    group: list[str] = []
    values: list[dict[str, Any]] = []
    all_axes: list[dict[str, Any]] = []
    stream: str = ""

    def add_axis(ax: dict[str, Any]) -> None:
        nonlocal stream
        cid = ax.get("columnId") or ax.get("id")
        if not cid:
            return
        axis = str(ax.get("axisName") or ax.get("axis") or "").upper()
        label = ax.get("customLabel") or ax.get("label") or ax.get("columnName") or ax.get("name") or ""
        name = ax.get("columnName") or ax.get("name") or ""
        all_axes.append({"axis": axis, "id": cid, "label": label, "name": name})
        if axis in _VALUE_AXES:
            values.append({"id": cid, "label": label, "name": name})
        else:
            group.append(cid)

    def walk(node: Any, in_visual: bool = False) -> None:
        nonlocal stream
        if isinstance(node, dict):
            here = in_visual or node.get("visualId") == target
            if node.get("visualId") == target:
                sid = node.get("preparedDataSourceId") or node.get("streamSourceId")
                if sid and not stream:
                    stream = str(sid)
            if here:
                for key in ("appliedAxes", "axes", "fieldWells", "columns"):
                    seq = node.get(key)
                    if isinstance(seq, list):
                        for ax in seq:
                            if isinstance(ax, dict):
                                add_axis(ax)
                sid = node.get("preparedDataSourceId") or node.get("streamSourceId")
                if sid and not stream:
                    stream = str(sid)
            for v in node.values():
                walk(v, here)
        elif isinstance(node, list):
            for it in node:
                walk(it, in_visual)

    walk(sheet_json)
    # De-dupe values / group by id, preserving order.
    seen: set[str] = set()
    values = [v for v in values if not (v["id"] in seen or seen.add(v["id"]))]
    seen2: set[str] = set()
    group = [g for g in group if not (g in seen2 or seen2.add(g))]
    return {"group": group, "values": values, "stream": stream, "all": all_axes}


def _match(col: dict[str, Any], keys: list[str]) -> bool:
    blob = f"{col.get('label', '')} {col.get('name', '')}".lower()
    return any(k in blob for k in keys)


def _build_start_vis(
    metric_specs: list[tuple[str, str]], group: list[str], stream_source_id: str,
) -> dict[str, Any]:
    """``metric_specs`` = list of ``(column_id, aggregation_func)`` e.g.
    ``[(cube_daily_num, "SUM"), (avg_ov_share, "AVG")]``."""
    group = list(group) or []
    last = len(group) - 1
    # The limited (last) dimension needs a sort config; sort by the first group
    # column (execution_date) DESC so the newest rows fall inside the limit.
    sort_col = group[0] if group else None
    fields = []
    for i, cid in enumerate(group):
        if i == last:
            fields.append({"name": cid, "limit": 50000,
                           "sort": {"name": sort_col, "dir": "DESC"} if sort_col else None,
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    metrics = [{"name": cid, "func": func, "chartContext": cid} for cid, func in metric_specs]
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
            "sheetId": SHEET_ID, "visualId": VISUAL_ID, "visualArn": None,
            "visualType": "TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def cube_login_status() -> dict[str, Any]:
    """Whether the QuickSight session can reach the Cube / OV% dashboard."""
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def _station_col_index(group: list[str], values_by_id: dict[str, dict]) -> int:
    """Best-effort index of the station dimension within the group tuple."""
    for i, cid in enumerate(group):
        low = str(cid).lower()
        if any(k in low for k in _STATION_KEYS):
            return i
    return 0


def fetch_cube_ov(
    *, station: str, date: str | None = None, timeout: float = 35.0,
    cookies: dict[str, str] | None = None, mbtc: str | None = None,
) -> dict[str, float]:
    """Return ``{"cube": <D-1 CPP>, "ov_pct": <D-1 OV%>}`` for ``station``.

    Reproduces the dashboard's calculated fields from physical columns
    (verified against DCZ4 for 2026-07-11 and 2026-07-12):

    * ``cube``   = SUM(cube_daily_num) / SUM(cube_metrics_den)  (D-1 CPP)
    * ``ov_pct`` = SUM(avg_ov_share) / OV_DIVISOR               (D-1 OV%)

    ``date`` selects the ``execution_date`` row to read — the dashboard shows
    the D0 (plan date − 1) column, so the Labor Calculator passes that date.
    Falls back to the latest available ``execution_date`` when omitted or when
    the requested date has no row yet."""
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta 'station'.")
    target = (date or "").strip()[:10] or None
    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    metric_specs = [
        (CPP_NUM_COL, "SUM"),
        (CPP_DEN_COL, "SUM"),
        (OV_SHARE_COL, "SUM"),
    ]
    data = _stream_vis(
        _build_start_vis(metric_specs, GROUP_COLS, STREAM_SOURCE_ID), cookies, mbtc, timeout,
    )
    if not data:
        raise QuickSightError("QuickSight no devolvio datos de Cube / OV%.")

    # Collect the station's rows keyed by execution_date, pick the requested
    # date (D0) when present, else the latest available.
    rows_by_date: dict[str, dict[str, Any]] = {}
    for item in data.get("data", []):
        g = item.get("group", [])
        st = str(g[_STATION_IDX]).strip().upper() if len(g) > _STATION_IDX else ""
        if st != station or not g:
            continue
        rows_by_date[str(g[0])[:10]] = item
    if not rows_by_date:
        return {}
    if target and target in rows_by_date:
        best_row = rows_by_date[target]
    else:
        best_row = rows_by_date[max(rows_by_date)]

    m = (best_row.get("current") or {}).get("metrics", {})
    num = _metric_value(m.get(CPP_NUM_COL))
    den = _metric_value(m.get(CPP_DEN_COL))
    share = _metric_value(m.get(OV_SHARE_COL))

    out: dict[str, float] = {}
    if num is not None and den:
        out["cube"] = float(num) / float(den)              # D-1 CPP
    if share is not None:
        out["ov_pct"] = float(share) / OV_DIVISOR           # D-1 OV%
    return out


# Native calculated-field column IDs of the pivot visual (from the dashboard
# metadata — labels resolved via /cube/debug keyword_objects). Reading these
# directly yields the EXACT dashboard values instead of reproducing them from
# physical columns. Overridable via env.
CPP_CALC_ID = os.getenv("QS_CUBE_CPP_CALC_ID", "").strip() or "fa2985d6-a697-48ab-beb1-5155435afaf6"  # D-1 CPP
OV_CALC_ID = os.getenv("QS_CUBE_OV_CALC_ID", "").strip() or "845682bf-64a2-4adc-a6f4-f44be8748b6f"    # D-1 OV%


def debug_cube_calc(
    *, station: str, date: str | None = None, timeout: float = 35.0,
) -> dict[str, Any]:
    """Diagnostic: read the native ``D-1 CPP`` / ``D-1 OV%`` calculated-field
    columns directly, trying each aggregation func, so we can see which one
    reproduces the dashboard value exactly (the calc field is already the final
    per-DS value repeated across the hidden dimension, so MIN/MAX/AVG should
    return it un-multiplied, unlike SUM).
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta 'station'.")
    target = (date or "").strip()[:10] or None
    cookies, mbtc = _open_session(timeout)

    out: dict[str, Any] = {
        "station": station, "target_date": target,
        "cpp_calc_id": CPP_CALC_ID, "ov_calc_id": OV_CALC_ID,
        "by_func": {},
    }
    for func in ("MIN", "MAX", "AVG", "SUM"):
        try:
            data = _stream_vis(
                _build_start_vis(
                    [(CPP_CALC_ID, func), (OV_CALC_ID, func)], GROUP_COLS, STREAM_SOURCE_ID,
                ),
                cookies, mbtc, timeout,
            )
        except Exception as exc:  # noqa: BLE001 - record and continue
            out["by_func"][func] = {"error": str(exc)[:200]}
            continue
        rows_by_date: dict[str, dict[str, Any]] = {}
        for item in (data or {}).get("data", []):
            g = item.get("group", [])
            st = str(g[_STATION_IDX]).strip().upper() if len(g) > _STATION_IDX else ""
            if st != station or not g:
                continue
            rows_by_date[str(g[0])[:10]] = item
        if not rows_by_date:
            out["by_func"][func] = {"error": "no rows for station"}
            continue
        pick = target if (target and target in rows_by_date) else max(rows_by_date)
        m = (rows_by_date[pick].get("current") or {}).get("metrics", {})
        out["by_func"][func] = {
            "date": pick,
            "cube": _metric_value(m.get(CPP_CALC_ID)),
            "ov_pct": _metric_value(m.get(OV_CALC_ID)),
            "dates_available": sorted(rows_by_date),
        }
    return out


def debug_cube_rows(
    *, station: str, timeout: float = 35.0,
) -> dict[str, Any]:
    """Diagnostic: dump every ``execution_date`` row for ``station`` with the
    raw physical metrics (num / den / share / count) and the values the tool
    computes from them (cube = num/den, ov_pct = share / count | divisor).

    Lets us compare, per date, what the tool reads against the dashboard so we
    can pin the right date / aggregation level / divisor without guessing.
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta 'station'.")
    cookies, mbtc = _open_session(timeout)

    metric_specs = [
        (CPP_NUM_COL, "SUM"),
        (CPP_DEN_COL, "SUM"),
        (OV_SHARE_COL, "SUM"),
    ]
    data = _stream_vis(
        _build_start_vis(metric_specs, GROUP_COLS, STREAM_SOURCE_ID), cookies, mbtc, timeout,
    )
    if not data:
        raise QuickSightError("QuickSight no devolvio datos de Cube / OV%.")

    rows: list[dict[str, Any]] = []
    for item in data.get("data", []):
        g = item.get("group", [])
        st = str(g[_STATION_IDX]).strip().upper() if len(g) > _STATION_IDX else ""
        if st != station:
            continue
        m = (item.get("current") or {}).get("metrics", {})
        num = _metric_value(m.get(CPP_NUM_COL))
        den = _metric_value(m.get(CPP_DEN_COL))
        share = _metric_value(m.get(OV_SHARE_COL))
        count = _metric_value(m.get("!calc.count.0.0"))
        cube = (float(num) / float(den)) if (num is not None and den) else None
        ov_by_div = (float(share) / OV_DIVISOR) if share is not None else None
        ov_by_count = (float(share) / float(count)) if (share is not None and count) else None
        rows.append({
            "group": [str(x) for x in g],
            "date": str(g[0])[:10] if g else None,
            "num": num, "den": den, "share": share, "count": count,
            "cube": cube,
            "ov_pct_by_divisor": ov_by_div,
            "ov_pct_by_count": ov_by_count,
        })
    rows.sort(key=lambda r: r.get("date") or "")
    return {
        "station": station,
        "ov_divisor_env": OV_DIVISOR,
        "cpp_num_col": CPP_NUM_COL,
        "cpp_den_col": CPP_DEN_COL,
        "ov_share_col": OV_SHARE_COL,
        "group_cols": GROUP_COLS,
        "station_idx": _STATION_IDX,
        "row_count": len(rows),
        "rows": rows,
    }


def _all_visual_ids(sheet_json: dict[str, Any]) -> list[str]:
    """Every ``visualId`` present in the sheet metadata (diagnostics)."""
    ids: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            vid = node.get("visualId")
            if isinstance(vid, str) and vid not in ids:
                ids.append(vid)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(sheet_json)
    return ids


def _fetch_list_filters(cookies: dict[str, str], mbtc: str, timeout: float) -> Any:
    """GET ListFiltersInSheetForDashboard — the payload that (per the HAR)
    carries the human-readable CPP / OV% column names."""
    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"
        f"?Operation=ListFiltersInSheetForDashboard&mbtc={mbtc}"
    )
    try:
        resp = requests.get(
            url, headers=_sheet_headers(mbtc), cookies=cookies, timeout=timeout,
            allow_redirects=False,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


_DEBUG_KEYS = ["cpp", "ov%", "ov ", "cube", "cost per", "cost_per", "overallocation",
               "over %", "over_", "d-1", "d_1", "d-1 "]


def _keyword_objects(node: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    """Collect small dicts whose serialized form mentions a target keyword.

    Surfaces calculated-field / column definitions (their id + name) so the
    operator can pin QS_CUBE_CPP_ID / QS_CUBE_OV_ID."""
    if len(out) >= 60:
        return
    if isinstance(node, dict):
        blob = json.dumps(node, ensure_ascii=False).lower()
        if any(k in blob for k in _DEBUG_KEYS) and len(blob) < 500:
            out.append(node)
        for v in node.values():
            _keyword_objects(v, out, depth + 1)
    elif isinstance(node, list):
        for it in node:
            _keyword_objects(it, out, depth + 1)


def debug_cube(*, timeout: float = 35.0) -> dict[str, Any]:
    """Diagnostic dump: visuals, axes and any object mentioning CPP / OV%.

    Used to configure the CPP / OV% matching (QS_CUBE_CPP_ID / QS_CUBE_OV_ID)."""
    cookies, mbtc = _open_session(timeout)
    sheet = _fetch_sheet_json(cookies, mbtc, timeout)
    if not sheet:
        raise QuickSightError("QuickSight no devolvio la hoja del dashboard (sesion?).")
    disc = _discover_visual(sheet)

    kw_objs: list[dict[str, Any]] = []
    _keyword_objects(sheet, kw_objs)
    filters = _fetch_list_filters(cookies, mbtc, timeout)
    if filters is not None:
        _keyword_objects(filters, kw_objs)

    return {
        "dashboard_id": DASHBOARD_ID,
        "sheet_id": SHEET_ID,
        "target_visual_id": VISUAL_ID,
        "visual_found": bool(disc.get("all")),
        "stream_source_id": disc.get("stream"),
        "group_ids": disc.get("group"),
        "value_columns": disc.get("values"),
        "all_axes": disc.get("all"),
        "all_visual_ids": _all_visual_ids(sheet),
        "keyword_objects": kw_objs,
    }


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "cube_login_status",
    "debug_cube",
    "debug_cube_calc",
    "debug_cube_rows",
    "fetch_cube_ov",
]
