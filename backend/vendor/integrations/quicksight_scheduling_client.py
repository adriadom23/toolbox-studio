"""QuickSight **Scheduling / OCM** dashboard integration.

Pulls the *Database*, *Scheduling* and *Allocation* tables out of the
``Scheduling Plans`` sheet of the EU AMZL Scheduling & OCM dashboard
(``5b821e9f-d9df-463b-92d0-1151c068704c``), reusing the exact same
Midway/QuickSight Firefox session machinery as
:mod:`app.integrations.quicksight_client` (the DPR dashboard client).

The dashboard layout (sheet/visual/column ids + the prepared data source
``streamSourceId`` per table) was reverse-engineered with
``backend/scripts/qs_discover.py`` and frozen into the versioned map
``quicksight_scheduling_map.json`` next to this module. Re-run the
discovery script and ``qs_build_map.py`` if the dashboard is republished
and the ids rotate.

Each table visual is a QuickSight TABLE whose rows stream over a
WebSocket. We connect with the session cookies, send a ``START_VIS``
message describing the visual (group dimensions + metric aggregations)
against the table's ``streamSourceId``, read the ``data`` frame and map
the column ids back to readable names.

Public surface:

* :func:`scheduling_login_status` — is the QuickSight session usable?
* :func:`fetch_table` — pull one table (``nd_scheduling`` ...).
* :func:`fetch_scheduling_bundle` — pull database + scheduling +
  allocation for a station in one shot (per program: ND / SD / AMXL).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

# Reuse the DPR client's dashboard-agnostic helpers (cookies, CSRF, the
# Set-Cookie / handshake-cookie parsers and the metric-cell decoder).
from app.integrations.quicksight_client import (
    QUICKSIGHT_HOST,
    QuickSightAuthError,
    QuickSightError,
    _extract_set_cookies,
    _fetch_mbtc as _dpr_fetch_mbtc,  # noqa: F401 (kept for parity / future use)
    _is_session_for_alias,
    _metric_value,
    _parse_handshake_cookies,
    _read_cookies,
    quicksight_login_status as _qs_login_status,  # noqa: F401 (kept for parity)
)

try:  # websocket-client
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None

import requests


_MAP_PATH = Path(__file__).with_name("quicksight_scheduling_map.json")
_MAP: dict[str, Any] = json.loads(_MAP_PATH.read_text(encoding="utf-8"))

DASHBOARD_ID: str = _MAP["dashboard_id"]
ALIAS: str = _MAP["alias"]
REGION: str = _MAP["region"]
SCHEDULING_SHEET_ID: str = _MAP["scheduling_sheet_id"]
HOST = QUICKSIGHT_HOST  # us-east-1.quicksight.aws.amazon.com

LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"

# key -> visual config (sheet_id, visual_id, stream_source_id, columns[...])
VISUALS: dict[str, dict[str, Any]] = {v["key"]: v for v in _MAP["visuals"]}

# Logical groupings used by :func:`fetch_scheduling_bundle`.
PROGRAM_TABLES: dict[str, dict[str, str]] = {
    "ND": {
        "database": "nd_database",
        "scheduling": "nd_scheduling",
        "allocation": "nd_allocation",
    },
    "SD": {
        "database": "sd_database",
        "scheduling": "sd_scheduling",
        "allocation": "sd_allocation",
    },
    "AMXL": {
        "scheduling": "amxl_scheduling_plan",
        "scheduling_routes": "amxl_scheduling_routes",
        "allocation": "amxl_allocation",
    },
}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)

# Canonical column-name overrides so ND and SD tables expose identical
# headers (the Database "Capacity GAP (pkgs)" column has a different
# truncated id per program). Keeps the ND+SD combined CSV consistent.
_CANONICAL_NAMES: dict[str, str] = {
    "capacity_c1196c": "capacity_gap",
    "capacity_d0f7a5": "capacity_gap",
}


def _base_columns(vis: dict[str, Any]) -> list[dict[str, Any]]:
    """Drop calculated fields (UUID ids) — they aren't queryable dimensions."""
    return [c for c in vis["columns"] if not _UUID_RE.match(c["id"])]


def _group_columns(vis: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in _base_columns(vis) if c["axis"] in ("GROUP_BY", "ROW", "COLUMN")]


def _metric_columns(vis: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in _base_columns(vis) if c["axis"] in ("VALUE", "METRIC")]


def _station_column(vis: dict[str, Any]) -> str | None:
    for c in _group_columns(vis):
        if c["name"] == "station":
            return c["id"]
    return None


def _page_url() -> str:
    return f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Read the live CSRF token from *this* dashboard's page."""
    import html as _html

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


def dashboard_login_status(
    *,
    dashboard_id: str,
    sheet_id: str,
    login_url: str,
    page_url: str,
) -> dict[str, Any]:
    """Whether the Firefox session can auto-fetch a *specific* dashboard.

    Each QuickSight tool must validate the session against the dashboard
    it actually reads from: a session that is perfectly valid for one
    dashboard returns HTTP 404 ("ClientKey ... not found") on another
    dashboard the account cannot see. Validating against the wrong
    dashboard therefore reports ``session_expired`` even with a live
    session — which is exactly what happened when this delegated to the
    shared DPR check.

    The CSRF token (``mbtc``) is read from ``page_url`` (the target
    dashboard's own page) and the liveness probe hits that dashboard's
    ``GetSheetForDashboard`` API.
    """
    base = {"host": HOST, "login_url": login_url, "dashboard_id": dashboard_id}

    cookies = _read_cookies()
    if not cookies:
        return {
            **base,
            "ready": False,
            "cookie_count": 0,
            "correct_account": False,
            "reason": "no_cookies",
        }

    correct_alias = _is_session_for_alias(cookies)

    # Read the live CSRF token from the target dashboard's own page. A
    # non-200 here means the session redirected to login (expired).
    import html as _html

    mbtc = ""
    try:
        page = requests.get(
            page_url,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies,
            timeout=12,
            allow_redirects=False,
        )
        if page.status_code == 200:
            m = re.search(r'"csrf"\s*:\s*"([^"]+)"', _html.unescape(page.text))
            mbtc = m.group(1) if m else ""
    except requests.RequestException:
        mbtc = ""

    if not mbtc:
        return {
            **base,
            "ready": False,
            "cookie_count": len(cookies),
            "correct_account": correct_alias,
            "reason": "wrong_account" if not correct_alias else "session_expired",
        }

    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{dashboard_id}"
        f"/sheets/{sheet_id}?Operation=GetSheetForDashboard&pageSize=1"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": page_url,
    }
    try:
        resp = requests.get(
            url, headers=headers, cookies=cookies, timeout=12, allow_redirects=False
        )
    except requests.RequestException:
        return {
            **base,
            "ready": False,
            "cookie_count": len(cookies),
            "correct_account": correct_alias,
            "reason": "network_error",
        }

    body = resp.text.lower()
    live = resp.status_code == 200 and "authenticate" not in body
    return {
        **base,
        "ready": live,
        "cookie_count": len(cookies),
        "correct_account": correct_alias,
        "reason": "" if live else ("wrong_account" if not correct_alias else "session_expired"),
    }


def scheduling_login_status() -> dict[str, Any]:
    """Whether the session is live for the Scheduling dashboard (``5b821e9f``)."""
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SCHEDULING_SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    """Trigger session bootstrap against this dashboard, capturing Set-Cookie."""
    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SCHEDULING_SHEET_ID}?Operation=GetSheetForDashboard&pageSize=175"
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


def _filter_in(col_id: str, values: list[str]) -> str:
    quoted = ",".join(f'"{v}"' for v in values)
    return f"((in({{{col_id}}},[{quoted}],true)))"


def _build_start_vis(
    vis: dict[str, Any],
    *,
    station: str | None,
    extra_filters: list[str] | None,
    limit: int,
) -> dict[str, Any]:
    group_cols = _group_columns(vis)
    metric_cols = _metric_columns(vis)
    if not group_cols:
        raise QuickSightError(f"Visual '{vis['key']}' sin dimensiones consultables.")

    # Sort so the most relevant rows fall inside QuickSight's ~2048 row cap.
    # When the table carries an ofd_date, sort by it DESC so recent/near-future
    # dates (where the D+1 plan lives) are returned first; otherwise sort by
    # the first dimension.
    ofd_col = next((c["id"] for c in group_cols if c["name"] in ("ofd_date", "OFD_date")), None)
    sort_col = ofd_col or group_cols[0]["id"]
    sort_dir = "DESC" if ofd_col else "ASC"
    last = len(group_cols) - 1
    fields = []
    for i, c in enumerate(group_cols):
        if i == last:
            fields.append({
                "name": c["id"], "limit": limit,
                "sort": {"name": sort_col, "dir": sort_dir},
                "chartContext": c["id"], "offset": 0, "partitioned": False,
            })
        else:
            fields.append({
                "name": c["id"], "limit": -1, "sort": None,
                "chartContext": c["id"], "offset": -1, "partitioned": False,
            })
    metrics = [
        {"name": c["id"], "func": (c.get("agg") or "SUM"), "chartContext": c["id"]}
        for c in metric_cols
    ]
    metrics.append({"name": "!calc.count.0.0", "func": "CUSTOM"})

    filters = list(extra_filters or [])
    if station:
        st_col = _station_column(vis)
        if st_col:
            filters.append(_filter_in(st_col, [station]))

    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metrics},
                "clsSupport": True,
                "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                "filterExpressions": filters,
                "parameters": {},
            },
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": vis["stream_source_id"], "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": vis["sheet_id"], "visualId": vis["visual_id"], "visualArn": None,
            "visualType": vis["type"], "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _connect_ws(cookies: dict[str, str], mbtc: str, timeout: float):
    ws_url = f"wss://{HOST}/sn/account/{ALIAS}/websocket/?mbtc={mbtc}"
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            return websocket.create_connection(
                ws_url,
                header=[f"User-Agent: {_UA}", f"Origin: https://{HOST}"],
                cookie=cookie_header, timeout=timeout, suppress_origin=True,
            ), cookies
        except Exception as exc:  # noqa: BLE001
            s = str(exc)
            if "(re)authenticate" in s or "return to https://console" in s:
                raise QuickSightAuthError(
                    "Sesion de AWS expirada. Reabre el dashboard en Firefox y reintenta."
                )
            if attempt == 0 and "403" in s:
                new = _parse_handshake_cookies(exc)
                if new:
                    cookies = {**cookies, **new}
                    continue
            if attempt == 0 and ("302" in s or "url is invalid" in s.lower()):
                raise QuickSightAuthError(
                    "Sesion de QuickSight no activa en la cuenta correcta. "
                    "Abre el dashboard en Firefox e inicia sesion."
                )
            raise QuickSightAuthError(f"No se pudo conectar al WebSocket: {s[:200]}")
    raise QuickSightAuthError("No se pudo conectar al WebSocket de QuickSight.")


def _latest_snapshot_rows(
    rows: list[dict[str, Any]], key_names: list[str]
) -> list[dict[str, Any]]:
    """Keep only the latest ``snapshot`` row per logical group.

    The Database dataset stores one row per pipeline snapshot; the dashboard
    hides the duplicates behind its "Latest Data Filter" control. Our
    WebSocket query cannot use that calculated filter, so without the
    ``snapshot`` dimension QuickSight SUMs the metrics across snapshots —
    e.g. two identical 2nd_run snapshots turned DSP Blended SPR 157 into
    314 and doubled OFD volume. We query WITH the snapshot dimension and
    collapse here to the newest snapshot per (station, cycle, ofd_date,
    uploadtyp, ...) group, mirroring the dashboard's Latest Data view.
    """
    best: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for row in rows:
        key = tuple(str(row.get(name) or "") for name in key_names)
        snap = str(row.get("snapshot") or "")
        current = best.get(key)
        if current is None:
            best[key] = row
            order.append(key)
        elif snap > str(current.get("snapshot") or ""):
            best[key] = row
    return [best[k] for k in order]


def _rows_from_frame(vis: dict[str, Any], data_msg: dict[str, Any]) -> list[dict[str, Any]]:
    group_cols = _group_columns(vis)
    metric_cols = _metric_columns(vis)
    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        g = item.get("group", [])
        mcells = (item.get("current") or {}).get("metrics", {})
        record: dict[str, Any] = {}
        for idx, c in enumerate(group_cols):
            name = _CANONICAL_NAMES.get(c["name"], c["name"])
            record[name] = g[idx] if idx < len(g) else None
        for c in metric_cols:
            name = _CANONICAL_NAMES.get(c["name"], c["name"])
            record[name] = _metric_value(mcells.get(c["id"]))
        rows.append(record)
    return rows


def _stream_vis(
    start_vis: dict[str, Any], cookies: dict[str, str], mbtc: str, timeout: float
) -> dict[str, Any] | None:
    """Open a WebSocket, send ``start_vis``, and return the first data frame.

    The QuickSight table visual streams rows in a single frame capped at
    ~2048 rows; callers that need more must narrow the query with filters.
    """
    ws, _ = _connect_ws(cookies, mbtc, timeout)
    cid = start_vis["cid"]
    data_msg = None
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
                raise QuickSightError(
                    f"QuickSight error: {msg.get('error')} - {msg.get('internalMessage')}"
                )
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
    return data_msg


def fetch_table(
    key: str,
    *,
    station: str | None = None,
    extra_filters: list[str] | None = None,
    timeout: float = 30.0,
    cookies: dict[str, str] | None = None,
    mbtc: str | None = None,
) -> list[dict[str, Any]]:
    """Pull one mapped table (``nd_scheduling``, ``sd_allocation`` ...).

    ``station`` is applied as a server-side filter when the table has a
    ``station`` dimension (Database / Scheduling). Allocation tables have
    no ``station`` column — the station code is embedded in ``serviceArea``
    — so for those callers pass an explicit ``serviceArea`` filter via
    ``extra_filters`` (see :func:`fetch_scheduling_bundle`).
    """
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")
    vis = VISUALS.get(key)
    if vis is None:
        raise QuickSightError(f"Tabla QuickSight desconocida: {key}")

    if cookies is None:
        cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{HOST}/ en Firefox."
        )
    if mbtc is None:
        mbtc = _fetch_mbtc(cookies, timeout=timeout)
        if not mbtc:
            raise QuickSightAuthError(
                "No se pudo leer el token CSRF. La sesion de QuickSight probablemente expiro."
            )
        cookies = _warm_up(cookies, mbtc, timeout)

    start_vis = _build_start_vis(
        vis, station=station, extra_filters=extra_filters,
        limit=int(vis.get("limit", 50000) or 50000),
    )
    data_msg = _stream_vis(start_vis, cookies, mbtc, timeout)
    if not data_msg:
        raise QuickSightError(f"QuickSight no devolvio datos para '{key}'.")
    rows = _rows_from_frame(vis, data_msg)
    group_names = [_CANONICAL_NAMES.get(c["name"], c["name"]) for c in _group_columns(vis)]
    if "snapshot" in group_names:
        rows = _latest_snapshot_rows(rows, [n for n in group_names if n != "snapshot"])
    return rows


def _service_area_column(vis: dict[str, Any]) -> str | None:
    for c in vis["columns"]:
        if c["name"] in ("servicear", "serviceArea"):
            return c["id"]
    return None


def _distinct_service_areas(
    vis: dict[str, Any], cookies: dict[str, str], mbtc: str, timeout: float
) -> tuple[list[str], str | None]:
    """Return ``(distinct_serviceAreas, area_column_id)`` for an allocation visual.

    A group-by-serviceArea-only query returns the distinct areas (a few
    hundred network-wide, well under the 2048 row cap), which we then match
    against the station code to build an exact ``in()`` filter."""
    area_col = _service_area_column(vis)
    if not area_col:
        return [], None
    start_vis = {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {
                    "fields": [{
                        "name": area_col, "limit": 5000,
                        "sort": {"name": area_col, "dir": "ASC"},
                        "chartContext": area_col, "offset": 0, "partitioned": False,
                    }],
                    "metrics": [{"name": "!calc.count.0.0", "func": "CUSTOM"}],
                },
                "clsSupport": True,
                "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                "filterExpressions": [], "parameters": {},
            },
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": vis["stream_source_id"], "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": vis["sheet_id"], "visualId": vis["visual_id"], "visualArn": None,
            "visualType": vis["type"], "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }
    data_msg = _stream_vis(start_vis, cookies, mbtc, timeout)
    areas: list[str] = []
    if data_msg:
        for item in data_msg.get("data", []):
            g = item.get("group", [])
            if g:
                areas.append(str(g[0]))
    return areas, area_col


def _allocation_area_filter(
    vis: dict[str, Any], station: str, cookies: dict[str, str], mbtc: str, timeout: float
) -> str | None:
    """Build a server-side ``in(serviceArea, [...])`` filter for ``station``.

    Returns ``None`` when the station has no matching serviceArea (the
    caller then skips the allocation fetch instead of pulling the global,
    truncated 2048-row page)."""
    areas, area_col = _distinct_service_areas(vis, cookies, mbtc, timeout)
    if not area_col or not areas:
        return None
    token = f"({station.upper()})"
    matched = [a for a in areas if token in a.upper()]
    if not matched:
        return None
    return _filter_in(area_col, matched)


def _allocation_matches_station(row: dict[str, Any], station: str) -> bool:
    """Allocation rows carry the station inside ``serviceArea`` (e.g.
    'Aachen (DNZ2) - Amazon Logistics ...'). Match on the ``(CODE)`` token."""
    area = str(row.get("servicearea") or row.get("serviceArea") or "")
    return f"({station.upper()})" in area.upper()


def _row_matches_plan_date(row: dict[str, Any], plan_date: str) -> bool:
    """Whether a fetched row belongs to ``plan_date`` (``YYYY-MM-DD``).

    Scheduling / Database rows expose an ``ofd_date`` timestamp; Allocation
    rows have split ``year`` / ``month`` / ``day`` columns. Rows without any
    date column are kept (cannot be filtered)."""
    ofd = str(row.get("ofd_date") or row.get("OFD_date") or "").strip()
    if ofd:
        return ofd[:10] == plan_date
    y, m, d = row.get("year"), row.get("month"), row.get("day")
    if y not in (None, "") and m not in (None, "") and d not in (None, ""):
        try:
            return f"{int(float(y)):04d}-{int(float(m)):02d}-{int(float(d)):02d}" == plan_date
        except (TypeError, ValueError):
            return False
    return True


def _allocation_date_filters(vis: dict[str, Any], plan_date: str) -> list[str]:
    """Server-side year/month/day ``in()`` filters for an allocation visual.

    Allocation rows carry the date split across numeric ``year`` / ``month`` /
    ``day`` columns (comparable types, so ``in()`` works, unlike the timestamp
    ofd_date used elsewhere)."""
    try:
        y, m, d = (int(x) for x in plan_date.split("-"))
    except (ValueError, AttributeError):
        return []
    ids = {c["name"]: c["id"] for c in vis["columns"]}
    out: list[str] = []
    if "year" in ids:
        out.append(f"((in({{{ids['year']}}},[{y}],true)))")
    if "month" in ids:
        out.append(f"((in({{{ids['month']}}},[{m}],true)))")
    if "day" in ids:
        out.append(f"((in({{{ids['day']}}},[{d}],true)))")
    return out


def _open_session(timeout: float = 30.0) -> tuple[dict[str, str], str]:
    """Read Firefox cookies + CSRF token and warm up the dashboard once.

    Returns ``(cookies, mbtc)`` ready to reuse across several
    :func:`fetch_table` / :func:`fetch_scheduling_bundle` calls so a
    "fetch everything" sweep doesn't re-authenticate per station. Raises
    :class:`QuickSightAuthError` when the session is unusable.
    """
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


def fetch_scheduling_bundle(
    *,
    station: str,
    program: str = "ND",
    plan_date: str | None = None,
    timeout: float = 30.0,
    cookies: dict[str, str] | None = None,
    mbtc: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Pull *database* + *scheduling* + *allocation* for ``station``.

    ``program`` selects the table family: ``ND`` (default), ``SD`` or
    ``AMXL``. ``plan_date`` (``YYYY-MM-DD``) filters each table to that
    operational date (ofd_date for scheduling/database, year/month/day for
    allocation). Reuses one cookie read + CSRF token + warm-up across the
    three WebSocket calls. Pass an existing ``cookies``/``mbtc`` pair (from
    :func:`_open_session`) to share one session across many stations.
    Tables that fail individually are reported under ``_errors`` instead of
    aborting the whole bundle.
    """
    program = (program or "ND").upper()
    tables = PROGRAM_TABLES.get(program)
    if not tables:
        raise QuickSightError(f"Programa desconocido: {program} (usa ND/SD/AMXL).")

    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    plan_date = (plan_date or "").strip() or None
    out: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for label, key in tables.items():
        try:
            if "allocation" in label and station:
                # Allocation has no station dimension and the visual is capped
                # at ~2048 rows, so a global pull truncates to the first area.
                # Resolve the station's exact serviceArea(s) and filter on them,
                # plus the numeric year/month/day when a plan date is given.
                vis = VISUALS[key]
                area_filter = _allocation_area_filter(vis, station, cookies, mbtc, timeout)
                if area_filter is None:
                    out[label] = []  # station has no allocation rows
                    continue
                filters = [area_filter]
                if plan_date:
                    filters.extend(_allocation_date_filters(vis, plan_date))
                rows = fetch_table(
                    key, station=None, extra_filters=filters,
                    timeout=timeout, cookies=cookies, mbtc=mbtc,
                )
                out[label] = rows
                continue
            rows = fetch_table(
                key, station=station, timeout=timeout, cookies=cookies, mbtc=mbtc,
            )
            if plan_date:
                rows = [r for r in rows if _row_matches_plan_date(r, plan_date)]
            out[label] = rows
        except Exception as exc:  # noqa: BLE001
            errors[label] = str(exc)
    if errors:
        out["_errors"] = errors  # type: ignore[assignment]
    return out


# Tables merged into the combined wave-plan CSVs (AMXL extras like
# ``scheduling_routes`` are pulled but not merged here).
_MERGE_KEYS = ("scheduling", "allocation", "database")


def fetch_scheduling_bundle_multi(
    *,
    stations: list[str],
    programs: list[str],
    plan_date: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Pull the scheduling bundle for many stations/programs in one session.

    Reuses a single Firefox cookie read + CSRF token + warm-up across every
    station × program so a "traer todo" sweep authenticates once instead of
    per station. Returns::

        {
            "tables": {"scheduling": [...], "allocation": [...], "database": [...]},
            "errors": ["DAB1/ND/allocation: ...", ...],
            "auth_error": bool,   # session died mid-sweep
            "message": str,       # only when auth_error
        }

    On :class:`QuickSightAuthError` the sweep stops but whatever was already
    fetched is preserved and returned with ``auth_error=True`` so the caller
    can still build a partial wave plan.
    """
    stations = [s.strip().upper() for s in (stations or []) if s and s.strip()]
    programs = [p.strip().upper() for p in (programs or []) if p and p.strip()] or ["ND"]
    plan_date = (plan_date or "").strip() or None

    merged: dict[str, list[dict[str, Any]]] = {k: [] for k in _MERGE_KEYS}
    errors: list[str] = []
    result: dict[str, Any] = {"tables": merged, "errors": errors, "auth_error": False}

    # Open one shared session; a failure here is a hard auth error.
    cookies, mbtc = _open_session(timeout)

    for st in stations:
        for prog in programs:
            try:
                bundle = fetch_scheduling_bundle(
                    station=st, program=prog, plan_date=plan_date,
                    timeout=timeout, cookies=cookies, mbtc=mbtc,
                )
            except QuickSightAuthError as exc:
                # Session expired mid-sweep: keep partial data, stop early.
                result["auth_error"] = True
                result["message"] = str(exc)
                return result
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{st}/{prog}: {exc}")
                continue
            for key in _MERGE_KEYS:
                rows = bundle.get(key)
                if isinstance(rows, list):
                    merged[key].extend(rows)
            for k, v in (bundle.get("_errors") or {}).items():
                errors.append(f"{st}/{prog}/{k}: {v}")
    return result


__all__ = [
    "DASHBOARD_ID",
    "HOST",
    "LOGIN_URL",
    "PROGRAM_TABLES",
    "VISUALS",
    "QuickSightAuthError",
    "QuickSightError",
    "_open_session",
    "fetch_scheduling_bundle",
    "fetch_scheduling_bundle_multi",
    "fetch_table",
    "scheduling_login_status",
]
