"""QuickSight integration for the **Station Bridge** (DO) tool.

Pulls the station-level performance data the weekly bridge needs from
three dashboards (all mapped with ``backend/scripts/qs_discover.py`` on
2026-08-12 and frozen into ``quicksight_bridge_map.json``):

* **scorecard** (``5e1897e0``) — "EU AMZL DSP Scorecard" per station.
  ``overview`` (Delivery Station Overview table: adjusted score + rank)
  and ``deep_dive`` (per-metric weekly pivot: score, actual, target,
  max score obtainable, target_score). The "Top 3 Opportunities" are
  derived in Python from the deep-dive rows (only metrics missing
  their target — signed ``target_score`` gap < 0 — ranked by that gap
  ascending; see ``station_bridge.select_top3``), replicating the
  dashboard's own "Performance Opportunity by Metric" table without
  depending on its calc-field UUIDs.
* **service_type** (``8fa1384c``) — SEU Service Type Compliance.
  Physical columns ``compliant`` / ``planned`` per ``station_code`` and
  ``date`` (STC % = compliant / planned).
* **dpr_compliance** (``52950eb4``) — DPR action items per station /
  week / DSP / status (compliance = items in a completed status over
  total).

Same Firefox-session machinery as every other QuickSight client in the
Toolbox; the dashboard-agnostic helpers are reused from
:mod:`app.integrations.quicksight_client` and the login-status probe
from :mod:`app.integrations.quicksight_scheduling_client`.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from app.integrations.quicksight_client import (
    QUICKSIGHT_HOST,
    QuickSightAuthError,
    QuickSightError,
    _extract_set_cookies,
    _is_session_for_alias,  # noqa: F401 (re-exported for the status probe)
    _parse_handshake_cookies,
    _read_cookies,
)


def _cell_value(cell: Any) -> float | None:
    """Decode a metric cell regardless of aggregation key.

    The shared ``quicksight_client._metric_value`` only reads ``sum`` /
    ``custom``; the scorecard deep-dive metrics aggregate with ``AVG`` so
    their cells come back keyed ``avg`` (same lesson as the roster client).
    """
    if not isinstance(cell, dict) or not cell:
        return None
    for key in ("custom", "sum", "avg", "average", "count", "min", "max"):
        if key in cell:
            val = cell[key]
            if isinstance(val, dict):
                val = val.get("parsedValue", val.get("source"))
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None
from app.integrations.quicksight_scheduling_client import dashboard_login_status

try:  # websocket-client
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None


def _map_path() -> Path:
    override = os.environ.get("QS_BRIDGE_MAP_FILE", "").strip()
    if override:
        candidate = Path(override)
        if candidate.exists():
            return candidate
    return Path(__file__).with_name("quicksight_bridge_map.json")


_MAP: dict[str, Any] = json.loads(_map_path().read_text(encoding="utf-8"))

ALIAS: str = _MAP["alias"]
HOST = QUICKSIGHT_HOST
DASHBOARDS: dict[str, dict[str, str]] = _MAP["dashboards"]
VISUALS: dict[str, dict[str, Any]] = {v["key"]: v for v in _MAP["visuals"]}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
_ROW_LIMIT = 50000


def dashboard_url(dashboard_key: str) -> str:
    dash = DASHBOARDS[dashboard_key]
    return f"https://{HOST}/sn/account/{ALIAS}/dashboards/{dash['dashboard_id']}"


def bridge_login_status(dashboard_key: str) -> dict[str, Any]:
    """Session liveness for one of the three mapped dashboards."""
    dash = DASHBOARDS[dashboard_key]
    url = dashboard_url(dashboard_key)
    return dashboard_login_status(
        dashboard_id=dash["dashboard_id"],
        sheet_id=dash["status_sheet_id"],
        login_url=url,
        page_url=url,
    )


# ---------------------------------------------------------------------------
# Session plumbing (per dashboard, since mbtc/warm-up are dashboard-scoped).
# ---------------------------------------------------------------------------


def _fetch_mbtc(dashboard_key: str, cookies: dict[str, str], timeout: float) -> str:
    import html as _html
    import re as _re

    try:
        resp = requests.get(
            dashboard_url(dashboard_key),
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
        )
        if resp.status_code != 200:
            return ""
        m = _re.search(r'"csrf"\s*:\s*"([^"]+)"', _html.unescape(resp.text))
        return m.group(1) if m else ""
    except requests.RequestException:
        return ""


def _warm_up(
    dashboard_key: str, sheet_id: str, cookies: dict[str, str], mbtc: str, timeout: float
) -> dict[str, str]:
    dash_id = DASHBOARDS[dashboard_key]["dashboard_id"]
    url = (
        f"https://{HOST}/sn/account/{ALIAS}/api/dashboards/{dash_id}"
        f"/sheets/{sheet_id}?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": dashboard_url(dashboard_key),
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


def _connect_ws(cookies: dict[str, str], mbtc: str, timeout: float):
    ws_url = f"wss://{HOST}/sn/account/{ALIAS}/websocket/?mbtc={mbtc}"
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            return (
                websocket.create_connection(  # type: ignore[union-attr]
                    ws_url,
                    header=[f"User-Agent: {_UA}", f"Origin: https://{HOST}"],
                    cookie=cookie_header,
                    timeout=timeout,
                    suppress_origin=True,
                ),
                cookies,
            )
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


# ---------------------------------------------------------------------------
# Generic visual fetch.
# ---------------------------------------------------------------------------


def _group_columns(vis: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in vis["columns"] if c["axis"] in ("GROUP_BY", "ROW", "COLUMN")]


def _metric_columns(vis: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in vis["columns"] if c["axis"] in ("VALUE", "METRIC")]


def _filter_in(col_id: str, values: list[str]) -> str:
    quoted = ",".join(f'"{v}"' for v in values)
    return f"((in({{{col_id}}},[{quoted}],true)))"


def _build_start_vis(vis: dict[str, Any], *, station: str | None) -> dict[str, Any]:
    dash_id = DASHBOARDS[vis["dashboard"]]["dashboard_id"]
    group_cols = _group_columns(vis)
    metric_cols = _metric_columns(vis)
    if not group_cols:
        raise QuickSightError(f"Visual '{vis['key']}' sin dimensiones consultables.")

    last = len(group_cols) - 1
    fields = []
    for i, c in enumerate(group_cols):
        if i == last:
            fields.append(
                {
                    "name": c["id"],
                    "limit": _ROW_LIMIT,
                    "sort": {"name": group_cols[0]["id"], "dir": "ASC"},
                    "chartContext": c["id"],
                    "offset": 0,
                    "partitioned": False,
                }
            )
        else:
            fields.append(
                {
                    "name": c["id"],
                    "limit": -1,
                    "sort": None,
                    "chartContext": c["id"],
                    "offset": -1,
                    "partitioned": False,
                }
            )

    metrics = [
        {"name": c["id"], "func": (c.get("agg") or "SUM"), "chartContext": c["id"]}
        for c in metric_cols
    ]
    calc_fields: list[dict[str, str]] = []
    if vis.get("count_rows"):
        # Row-count metric for visuals whose measures are calc-field UUIDs
        # (not queryable at the SPICE layer): we count rows per group in
        # QuickSight and aggregate in Python (same trick as the Scheduling
        # client's ``!calc.count.0.0``).
        metrics.append({"name": "!calc.count.0.0", "func": "CUSTOM"})
        calc_fields.append({"alias": "!calc.count.0.0", "func": "countOver(1)"})

    filters: list[str] = []
    if station and vis.get("station_column"):
        filters.append(_filter_in(vis["station_column"], [station]))

    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": dash_id,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metrics},
                "clsSupport": True,
                "calculatedFields": calc_fields,
                "filterExpressions": filters,
                "parameters": {},
            },
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": "Europe/Madrid",
            "streamSourceId": vis["stream_source_id"],
            "datasetVersionAlias": "$STABLE",
            "enableComputations": False,
            "computationsOnly": False,
            "sheetId": vis["sheet_id"],
            "visualId": vis["visual_id"],
            "visualArn": None,
            "visualType": vis["type"],
            "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()),
            "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _stream_vis(
    start_vis: dict[str, Any], cookies: dict[str, str], mbtc: str, timeout: float
) -> dict[str, Any] | None:
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
            except Exception:  # noqa: BLE001
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            pass
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    return data_msg


def _rows_from_frame(vis: dict[str, Any], data_msg: dict[str, Any]) -> list[dict[str, Any]]:
    group_cols = _group_columns(vis)
    metric_cols = _metric_columns(vis)
    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        g = item.get("group", [])
        mcells = (item.get("current") or {}).get("metrics", {})
        record: dict[str, Any] = {}
        for idx, c in enumerate(group_cols):
            record[c["name"]] = g[idx] if idx < len(g) else None
        for c in metric_cols:
            record[c["name"]] = _cell_value(mcells.get(c["id"]))
        if vis.get("count_rows"):
            record["row_count"] = _cell_value(mcells.get("!calc.count.0.0"))
        rows.append(record)
    return rows


class BridgeSession:
    """One QuickSight session reused across visual fetches per dashboard."""

    def __init__(self, timeout: float = 30.0) -> None:
        if websocket is None:
            raise QuickSightError("Falta la dependencia 'websocket-client'.")
        self.timeout = timeout
        base = _read_cookies()
        if not base:
            raise QuickSightAuthError(
                f"No hay cookies de QuickSight. Inicia sesion en https://{HOST}/ en Firefox."
            )
        self._base_cookies = base
        # dashboard_key -> (cookies, mbtc)
        self._sessions: dict[str, tuple[dict[str, str], str]] = {}

    def _session_for(self, vis: dict[str, Any]) -> tuple[dict[str, str], str]:
        key = vis["dashboard"]
        if key not in self._sessions:
            mbtc = _fetch_mbtc(key, self._base_cookies, self.timeout)
            if not mbtc:
                raise QuickSightAuthError(
                    "No se pudo leer el token CSRF de QuickSight. La sesion probablemente expiro."
                )
            cookies = _warm_up(key, vis["sheet_id"], self._base_cookies, mbtc, self.timeout)
            self._sessions[key] = (cookies, mbtc)
        return self._sessions[key]

    def fetch(self, visual_key: str, *, station: str | None = None) -> list[dict[str, Any]]:
        vis = VISUALS.get(visual_key)
        if vis is None:
            raise QuickSightError(f"Visual QuickSight desconocido: {visual_key}")
        cookies, mbtc = self._session_for(vis)
        start_vis = _build_start_vis(vis, station=station)
        data_msg = _stream_vis(start_vis, cookies, mbtc, self.timeout)
        if not data_msg:
            raise QuickSightError(f"QuickSight no devolvio datos para '{visual_key}'.")
        return _rows_from_frame(vis, data_msg)


__all__ = [
    "ALIAS",
    "DASHBOARDS",
    "VISUALS",
    "BridgeSession",
    "QuickSightAuthError",
    "QuickSightError",
    "bridge_login_status",
    "dashboard_url",
]
