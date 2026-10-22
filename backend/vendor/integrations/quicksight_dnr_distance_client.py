"""QuickSight integration for the **DNR Concessions Deep Dive** table.

Pulls the per-``tracking_id`` *Scan Distance Bucket* straight from the
"DNR Concessions Deep Dive" dashboard so the weekly DNRs tool can enrich
every tracking with its scan distance (the distance between the delivery
scan and the customer address). It reuses the exact same Midway/QuickSight
Firefox session, ``mbtc`` handshake and WebSocket ``START_VIS`` mechanism
already proven for the carrier-investigation and DPR clients.

All ids below were mapped from a captured session HAR of the dashboard
(``us-east-1.quicksight.aws.amazon.com`` page export):

* dashboard            ``73748978-6eea-425a-92bc-e3a13e1c0ab0``
* sheet                ``…_287e1b82-0a67-40bc-8113-d69deb019ef4``
* table visual         ``…_af2ce448-53dc-4de1-849a-9b9646f398f7``
  ("Table 1 - DNR Concessions Deep Dive")
* prepared data source ``9b7b26cd-398c-4468-bbdb-8790d7b96ebc`` (streamSourceId)
* physical table id    ``b0421479-d3a1-44b5-b081-8a92937a037a`` (column prefix)

The dashboard's **"Scan Distance Bucket"** is an analysis calculated field
(``7d89e5bb-a504-4adc-81ec-cb066d34f052``) defined as::

    ifelse(isnull({distance_meters}), 'No data',
           ifelse({distance_meters} > 25, 'Over 25m', 'Under 25m'))

Rather than ship that calc field in the query, we read the physical
``distance_meters`` per tracking and reproduce the bucket locally (see
:func:`distance_bucket`), which keeps the ``START_VIS`` payload identical
in shape to the carrier client (no analysis calc fields).

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
    os.environ.get("DNR_DISTANCE_QS_DASHBOARD_ID", "").strip()
    or "73748978-6eea-425a-92bc-e3a13e1c0ab0"
)
SHEET_ID = (
    os.environ.get("DNR_DISTANCE_QS_SHEET_ID", "").strip()
    or "73748978-6eea-425a-92bc-e3a13e1c0ab0_287e1b82-0a67-40bc-8113-d69deb019ef4"
)
VISUAL_ID = (
    os.environ.get("DNR_DISTANCE_QS_VISUAL_ID", "").strip()
    or "73748978-6eea-425a-92bc-e3a13e1c0ab0_af2ce448-53dc-4de1-849a-9b9646f398f7"
)
DEFAULT_STREAM_SOURCE_ID = (
    os.environ.get("DNR_DISTANCE_QS_STREAM_SOURCE_ID", "").strip()
    or "9b7b26cd-398c-4468-bbdb-8790d7b96ebc"
)

# Prefix shared by every physical table column on this prepared data source.
_COL_PREFIX = (
    os.environ.get("DNR_DISTANCE_QS_COL_PREFIX", "").strip()
    or "b0421479-d3a1-44b5-b081-8a92937a037a"
)
_TRACKING_ID = f"{_COL_PREFIX}.tracking_id"
_DISTANCE_ID = (
    os.environ.get("DNR_DISTANCE_QS_DISTANCE_COL", "").strip()
    or f"{_COL_PREFIX}.distance_meters"
)

# Aggregation used when a tracking_id has more than one concession row. The
# closest scan (MIN) is the most relevant for the DNR investigation, but the
# func can be re-pointed via env (MIN/MAX/AVG).
_DISTANCE_FUNC = os.environ.get("DNR_DISTANCE_QS_FUNC", "").strip() or "MIN"

# The dashboard's "Scan Distance Bucket" threshold (metres). distance > this
# => "Over 25m", else "Under 25m"; null => "No data".
_BUCKET_THRESHOLD_M = 25.0
try:
    _BUCKET_THRESHOLD_M = float(
        os.environ.get("DNR_DISTANCE_QS_BUCKET_THRESHOLD", "").strip() or "25"
    )
except ValueError:
    _BUCKET_THRESHOLD_M = 25.0

BUCKET_NO_DATA = "No data"
BUCKET_UNDER = "Under 25m"
BUCKET_OVER = "Over 25m"

_DEFAULT_MBTC = "b6e3n16w1F1SM%2Fd6%2FwtLU7mtC5IHnwPAcAtBpBchRgc%3D"

DNR_DISTANCE_LOGIN_URL = (
    f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}"
)

# QuickSight rejects very long ``in(...)`` filter lists, so we query the
# tracking ids in batches and merge the results.
_TRACKING_BATCH = 400
_LIMIT = 50000

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"


def distance_bucket(distance_m: float | None) -> str:
    """Reproduce the dashboard's "Scan Distance Bucket" calculated field."""
    if distance_m is None:
        return BUCKET_NO_DATA
    return BUCKET_OVER if distance_m > _BUCKET_THRESHOLD_M else BUCKET_UNDER


def _cell_value(cell: Any) -> float | None:
    """Read a metric cell regardless of aggregation key (min/max/sum/...)."""
    if not isinstance(cell, dict) or not cell:
        return None
    for key in ("min", "max", "sum", "avg", "average", "count", "custom"):
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
    return (
        os.environ.get("DNR_DISTANCE_QS_STREAM_SOURCE_ID", "").strip()
        or DEFAULT_STREAM_SOURCE_ID
    )


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Read the live per-session csrf token from the dashboard HTML page."""
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
    """Bootstrap the session against the dashboard (mirrors carrier client)."""
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


def dnr_distance_login_status() -> dict[str, Any]:
    """Check whether the QuickSight session can read the distance dashboard."""
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


def _build_start_vis(trackings: list[str]) -> dict[str, Any]:
    """START_VIS that groups by tracking_id and aggregates distance_meters.

    We request a minimal projection (one dimension + one measure) scoped to
    the supplied tracking ids, exactly like the carrier client builds its
    own group/metrics instead of replaying the full table visual.
    """
    fields = [
        {
            "name": _TRACKING_ID, "limit": _LIMIT,
            "sort": {"name": _TRACKING_ID, "dir": "ASC"},
            "chartContext": _TRACKING_ID, "offset": 0, "partitioned": False,
        }
    ]
    metrics = [
        {"name": _DISTANCE_ID, "func": _DISTANCE_FUNC, "chartContext": _DISTANCE_ID}
    ]
    filters: list[str] = []
    if trackings:
        filters.append(f'((in({{{_TRACKING_ID}}},[{_filter_list(trackings)}],true)))')

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


def _open_socket(cookies: dict[str, str], mbtc: str, timeout: float):
    ws_url = f"wss://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/websocket/?mbtc={mbtc}"
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            return websocket.create_connection(
                ws_url,
                header=[f"User-Agent: {_UA}", "Origin: https://" + QUICKSIGHT_HOST],
                cookie=cookie_header, timeout=timeout, suppress_origin=True,
            ), cookies
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
    raise QuickSightAuthError("No se pudo conectar al WebSocket de QuickSight.")


def _run_one(ws, trackings: list[str], timeout: float, debug: dict | None) -> dict[str, float | None]:
    start_vis = _build_start_vis(trackings)
    cid = start_vis["cid"]
    if debug is not None:
        debug.setdefault("start_vis", start_vis)
        debug.setdefault("frames", [])
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
        except Exception:
            pass

    out: dict[str, float | None] = {}
    if not data_msg:
        return out
    for item in data_msg.get("data", []):
        g = item.get("group", [])
        tid = g[0] if g else None
        if not tid:
            continue
        mcells = (item.get("current") or {}).get("metrics", {})
        out[str(tid).strip().upper()] = _cell_value(mcells.get(_DISTANCE_ID))
    return out


def fetch_distance_buckets(
    trackings: list[str],
    *,
    timeout: float = 25.0,
    debug: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Pull the scan distance + bucket for the given tracking ids.

    Returns ``{tracking_id: {"distance_m": float|None, "bucket": str}}`` for
    every tracking QuickSight knows about. Raises :class:`QuickSightAuthError`
    when the Firefox session is missing/expired.
    """
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")

    clean = sorted({str(t).strip().upper() for t in (trackings or []) if str(t or "").strip()})
    if not clean:
        return {}

    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{QUICKSIGHT_HOST}/ en Firefox."
        )
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    cookies = _warm_up(cookies, mbtc, timeout)

    distances: dict[str, float | None] = {}
    batches = [clean[i:i + _TRACKING_BATCH] for i in range(0, len(clean), _TRACKING_BATCH)]
    for batch in batches:
        ws, cookies = _open_socket(cookies, mbtc, timeout)
        try:
            distances.update(_run_one(ws, batch, timeout, debug))
        finally:
            try:
                ws.close()
            except Exception:
                pass

    result: dict[str, dict[str, Any]] = {}
    for tid, dist in distances.items():
        result[tid] = {"distance_m": dist, "bucket": distance_bucket(dist)}
    return result


__all__ = [
    "DASHBOARD_ID",
    "DNR_DISTANCE_LOGIN_URL",
    "BUCKET_NO_DATA",
    "BUCKET_UNDER",
    "BUCKET_OVER",
    "QuickSightAuthError",
    "QuickSightError",
    "distance_bucket",
    "dnr_distance_login_status",
    "fetch_distance_buckets",
    "fetch_concession_rows",
    "CONCESSION_COLUMNS",
]


# ---------------------------------------------------------------------------
# Full concession deep-dive projection.
#
# Every column the operator listed maps to a PHYSICAL column on the
# ``b0421479-...`` prepared data source (verified from the dashboard HAR);
# "Scan Distance Bucket" is the only derived value and we reproduce it from
# ``distance_meters`` via :func:`distance_bucket`. We group by all the
# dimensions (tracking_id LAST so QuickSight paginates on it) and aggregate
# the three numeric measures, mirroring the "Table 1 - DNR Concessions Deep
# Dive" visual.
# ---------------------------------------------------------------------------

# (output_key, physical_column_suffix) — order defines the group array index.
_DIMS: list[tuple[str, str]] = [
    ("country", "country"),
    ("week", "week"),
    ("node", "node"),
    ("shipment_reason", "shipment_reason"),
    ("order_id", "order_id"),
    ("pid", "pid"),
    ("postcode", "postcode"),
    ("address_type", "address_type"),
    ("destination_address_id", "destination_address_id"),
    ("concession_day", "day"),
    ("concession_bucket_l3", "concession_bucket_l3"),
    ("bucket2", "bucket2"),
    ("concession_creation_datetime", "concession_creation_datetime"),
    ("repeat_address", "repeat_address"),
    ("repeat_driver", "repeat_driver"),
    ("repeat_customer", "repeat_customer"),
    ("delivery_datetime", "actual_delivery_datetime"),
    ("da_tenure", "da_tenure"),
    ("concession_id", "concession_id"),
    ("distance_meters", "distance_meters"),
    ("is_amazon_business", "is_amazon_business"),
    ("repeat_aid", "repeat_aid"),
    ("repeat_da", "repeat_da"),
    ("repeat_pid", "repeat_pid"),
    ("transporter_id", "transporter_id"),
    ("tracking_id", "tracking_id"),  # LAST -> paginated field
]

# (output_key, physical_column_suffix, aggregation_func)
_MEASURES: list[tuple[str, str, str]] = [
    ("concession_units", "concession_units", "SUM"),
    ("concession_cost", "concession_cost", "SUM"),
]

# Ordered list of every output column produced per row (handy for callers
# that want to render/export the full projection, e.g. the weekly Excel).
CONCESSION_COLUMNS: list[str] = (
    [out for out, _ in _DIMS]
    + [out for out, _, _ in _MEASURES]
    + ["scan_distance_bucket"]
)

_NODE_ID = f"{_COL_PREFIX}.node"
_WEEK_ID = f"{_COL_PREFIX}.week"


def _col(suffix: str) -> str:
    return f"{_COL_PREFIX}.{suffix}"


def _build_concession_start_vis(
    stations: list[str], weeks: list[str], trackings: list[str]
) -> dict[str, Any]:
    last = len(_DIMS) - 1
    first_id = _col(_DIMS[0][1])
    fields = []
    for i, (_out, suffix) in enumerate(_DIMS):
        cid = _col(suffix)
        if i == last:
            fields.append({
                "name": cid, "limit": _LIMIT,
                "sort": {"name": first_id, "dir": "ASC"},
                "chartContext": cid, "offset": 0, "partitioned": False,
            })
        else:
            fields.append({
                "name": cid, "limit": -1, "sort": None,
                "chartContext": cid, "offset": -1, "partitioned": False,
            })
    metrics = [
        {"name": _col(suffix), "func": func, "chartContext": _col(suffix)}
        for _out, suffix, func in _MEASURES
    ]

    filters: list[str] = []
    if stations:
        filters.append(f'((in({{{_NODE_ID}}},[{_filter_list(stations)}],true)))')
    if weeks:
        filters.append(f'((in({{{_WEEK_ID}}},[{_filter_list(weeks)}],true)))')
    if trackings:
        filters.append(f'((in({{{_TRACKING_ID}}},[{_filter_list(trackings)}],true)))')

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


def _collect(ws, start_vis: dict, timeout: float, debug: dict | None):
    cid = start_vis["cid"]
    if debug is not None:
        debug.setdefault("start_vis", start_vis)
        debug.setdefault("frames", [])
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
        except Exception:
            pass
    return data_msg


def fetch_concession_rows(
    *,
    stations: list[str] | None = None,
    weeks: list[str] | None = None,
    trackings: list[str] | None = None,
    timeout: float = 30.0,
    debug: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pull the full DNR Concessions Deep Dive projection from QuickSight.

    Returns one dict per concession row with every column in
    :data:`CONCESSION_COLUMNS` (plus the derived ``scan_distance_bucket``).
    Scope by ``stations`` (the ``node`` column) and/or ``weeks`` (the
    ``week`` column) and/or an explicit ``trackings`` list. Raises
    :class:`QuickSightAuthError` when the Firefox session is missing/expired.
    """
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")

    stations = [str(s).strip().upper() for s in (stations or []) if str(s or "").strip()]
    weeks = [str(w).strip() for w in (weeks or []) if str(w or "").strip()]
    trk = sorted({str(t).strip().upper() for t in (trackings or []) if str(t or "").strip()})

    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{QUICKSIGHT_HOST}/ en Firefox."
        )
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    cookies = _warm_up(cookies, mbtc, timeout)

    # Tracking ids in batches (long in(...) lists are rejected); other scopes
    # run in a single pass.
    batches = (
        [trk[i:i + _TRACKING_BATCH] for i in range(0, len(trk), _TRACKING_BATCH)]
        if trk else [[]]
    )

    rows: list[dict[str, Any]] = []
    seen_keys: set[tuple] = set()
    raw_items = 0
    for batch in batches:
        ws, cookies = _open_socket(cookies, mbtc, timeout)
        try:
            data_msg = _collect(
                ws, _build_concession_start_vis(stations, weeks, batch), timeout, debug
            )
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if not data_msg:
            continue
        for item in data_msg.get("data", []):
            raw_items += 1
            g = item.get("group", [])
            mcells = (item.get("current") or {}).get("metrics", {})
            rec: dict[str, Any] = {}
            for idx, (out, _suffix) in enumerate(_DIMS):
                rec[out] = (g[idx] if idx < len(g) else None)
            for out, suffix, _func in _MEASURES:
                val = _cell_value(mcells.get(_col(suffix)))
                rec[out] = val
            # Derived bucket from distance_meters.
            dist = rec.get("distance_meters")
            try:
                dist_f = float(dist) if dist not in (None, "") else None
            except (TypeError, ValueError):
                dist_f = None
            rec["scan_distance_bucket"] = distance_bucket(dist_f)
            # De-dup on the FULL row tuple so two distinct concessions that
            # happen to share concession_id + tracking_id (e.g. blank ids)
            # are never dropped — only byte-identical rows collapse. The old
            # (concession_id, tracking_id) key silently lost such rows.
            key = (
                tuple(rec.get(out) for out, _ in _DIMS)
                + tuple(rec.get(out) for out, _suffix, _func in _MEASURES)
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(rec)
    if debug is not None:
        debug["raw_items"] = raw_items
        debug["deduped_rows"] = len(rows)
    return rows
