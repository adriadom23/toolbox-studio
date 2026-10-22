"""QuickSight **BAT weekly planning** integration (Nostradamus).

Pulls the "Value aggregation" pivot of the AMZL EU BAT dashboard
(``231ee670-…``) straight from QuickSight, reusing the Midway session
cookies stored in the operator's Firefox profile — same philosophy as the
Cube / Scheduling / Roster clients.

The captured HARs carry no WebSocket frames, so (like the roster client)
everything was discovered at runtime via ``GetSheetForDashboard`` plus a
schema probe (see ``backend/scripts/nostradamus_qs_probe*.py``):

* The pivot's row label (" ") and its metric (``value_aggregation``) are
  ANALYSIS calculated fields — not queryable directly. The underlying
  prepared data source *is*: it stores one row per (station, cycle, metric
  name, OFD date) with the metric name in ``ColumnId-17`` and the value in
  ``ColumnId-18``.
* Values validated against the operator's manual CSV export for
  DCZ4 · 2026-32 · cycle_1: sideline_out Sun = 2 916.5, dispatch volume
  Mon = 29 772.9, available_mechanical_capacity Sun = 58 050.

We group by (metric name × OFD date), filter by station + week + cycle
server-side, and request SUM / MIN / MAX / AVG of the value column in one
query so the caller can pick the right aggregation per metric (additive
volumes → SUM; per-day targets like ``spr_target`` that repeat across
hidden sub-dimensions → MIN/AVG).

Env overrides (rotate-safe, mirrors the other QuickSight clients):
    QS_NOSTRA_DASHBOARD_ID, QS_NOSTRA_SHEET_ID, QS_NOSTRA_VISUAL_ID,
    QS_NOSTRA_STREAM_SOURCE_ID, QS_NOSTRA_DATASET_ID,
    QS_NOSTRA_COL_STATION, QS_NOSTRA_COL_CYCLE, QS_NOSTRA_COL_WEEK,
    QS_NOSTRA_COL_DATE, QS_NOSTRA_COL_METRIC, QS_NOSTRA_COL_VALUE
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
    _read_cookies,
    _stream_vis,
    dashboard_login_status,
)

# ---------------------------------------------------------------------------
# IDs (discovered 2026-08-01 via GetSheetForDashboard; env-overridable)
# ---------------------------------------------------------------------------
DASHBOARD_ID = os.getenv("QS_NOSTRA_DASHBOARD_ID", "").strip() or (
    "231ee670-720b-4702-b373-b5ed2db41bfd"
)
SHEET_ID = os.getenv("QS_NOSTRA_SHEET_ID", "").strip() or (
    f"{DASHBOARD_ID}_4b419a1c-b820-43b0-9955-4097a1e9bd2b"
)
VISUAL_ID = os.getenv("QS_NOSTRA_VISUAL_ID", "").strip() or (
    f"{DASHBOARD_ID}_00f16da5-bee1-4f69-b28a-3a561c4fcde7"
)
STREAM_SOURCE_ID = os.getenv("QS_NOSTRA_STREAM_SOURCE_ID", "").strip() or (
    "b4e839e4-8f33-4303-8449-088fd0d4f105"
)

_DS = os.getenv("QS_NOSTRA_DATASET_ID", "").strip() or "eb85c903-936b-4bc1-ab3c-7934b6a09845"

COL_STATION = os.getenv("QS_NOSTRA_COL_STATION", "").strip() or f"{_DS}.ColumnId-7"
COL_CYCLE = os.getenv("QS_NOSTRA_COL_CYCLE", "").strip() or f"{_DS}.ColumnId-9"
COL_WEEK = os.getenv("QS_NOSTRA_COL_WEEK", "").strip() or f"{_DS}.ColumnId-12"
COL_DATE = os.getenv("QS_NOSTRA_COL_DATE", "").strip() or f"{_DS}.ColumnId-13"
COL_METRIC = os.getenv("QS_NOSTRA_COL_METRIC", "").strip() or f"{_DS}.ColumnId-17"
COL_VALUE = os.getenv("QS_NOSTRA_COL_VALUE", "").strip() or f"{_DS}.ColumnId-18"

DEFAULT_CYCLE = "cycle_1"

LOGIN_URL = f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"

_WEEK_RE = re.compile(r"^\d{4}-\d{2}$")


def _page_url() -> str:
    return LOGIN_URL


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Read the live CSRF token from this dashboard's page."""
    try:
        resp = requests.get(
            _page_url(),
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
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
        "User-Agent": _UA,
        "Accept": "*/*",
        "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": _page_url(),
    }


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    """Trigger the session bootstrap against this dashboard (Set-Cookie capture)."""
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


def nostradamus_login_status() -> dict[str, Any]:
    """Whether the Firefox session can auto-fetch the BAT dashboard."""
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_page_url(),
    )


def _filter_in(col_id: str, values: list[str]) -> str:
    quoted = ",".join(f'"{v}"' for v in values)
    return f"((in({{{col_id}}},[{quoted}],true)))"


def _build_start_vis(*, station: str, weeks: list[str], cycle: str) -> dict[str, Any]:
    """Group by (metric name × OFD date) with SUM/MIN/MAX/AVG of the value.

    The four aggregations ride in ONE frame: QuickSight keys the response
    metrics dict by column id, each entry carrying ``{"sum":…, "min":…}``
    per requested func.
    """
    fields = [
        {
            "name": COL_METRIC, "limit": -1, "sort": None,
            "chartContext": COL_METRIC, "offset": -1, "partitioned": False,
        },
        {
            "name": COL_DATE, "limit": 5000,
            "sort": {"name": COL_DATE, "dir": "ASC"},
            "chartContext": COL_DATE, "offset": 0, "partitioned": False,
        },
    ]
    metrics = [
        {"name": COL_VALUE, "func": "SUM", "chartContext": COL_VALUE},
        {"name": COL_VALUE, "func": "MIN", "chartContext": COL_VALUE},
        {"name": COL_VALUE, "func": "MAX", "chartContext": COL_VALUE},
        {"name": COL_VALUE, "func": "AVG", "chartContext": COL_VALUE},
        {"name": "!calc.count.0.0", "func": "CUSTOM"},
    ]
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metrics},
                "clsSupport": True,
                "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                "filterExpressions": [
                    _filter_in(COL_STATION, [station]),
                    _filter_in(COL_WEEK, weeks),
                    _filter_in(COL_CYCLE, [cycle]),
                ],
                "parameters": {},
            },
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": STREAM_SOURCE_ID, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": SHEET_ID, "visualId": VISUAL_ID, "visualArn": None,
            "visualType": "TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _agg_values(entry: Any) -> dict[str, float]:
    """Normalise a response metrics entry into ``{"sum":…, "min":…, ...}``."""
    out: dict[str, float] = {}
    if isinstance(entry, dict):
        for key, value in entry.items():
            try:
                if value is not None:
                    out[str(key).lower()] = float(value)
            except (TypeError, ValueError):
                continue
    elif entry is not None:
        try:
            out["sum"] = float(entry)
        except (TypeError, ValueError):
            pass
    return out


def fetch_week(
    *,
    station: str,
    week: str | list[str],
    cycle: str = DEFAULT_CYCLE,
    timeout: float = 35.0,
    cookies: dict[str, str] | None = None,
    mbtc: str | None = None,
) -> list[dict[str, Any]]:
    """Return ``[{"metric", "date", "values": {sum,min,max,avg}}]``.

    * ``metric`` is the RAW dataset metric name (``sideline_out``,
      ``spr_target``, ``dispatch_volume_uncapped``, ``routes_demand_flex``…).
    * ``date`` is ``YYYY-MM-DD``.
    * ``week`` must be the Amazon week label ``YYYY-WW`` (zero-padded) — or a
      LIST of labels: in daily mode D+1 can cross into the next week
      (Saturday → Sunday), so both weeks ride in ONE query (the ``filterIn``
      expression takes a value list natively; rows carry the date, so the
      caller keys by ISO date and absorbs them transparently).
    """
    station = (station or "").strip().upper()
    weeks = [week] if isinstance(week, str) else [str(w) for w in (week or [])]
    weeks = list(dict.fromkeys(w.strip() for w in weeks if str(w).strip()))
    cycle = (cycle or DEFAULT_CYCLE).strip().lower()
    if not station:
        raise QuickSightError("Falta 'station'.")
    if not weeks or any(not _WEEK_RE.match(w) for w in weeks):
        raise QuickSightError("'week' debe tener formato YYYY-WW (p.ej. 2026-32).")

    if cookies is None or mbtc is None:
        cookies, mbtc = _open_session(timeout)

    data = _stream_vis(
        _build_start_vis(station=station, weeks=weeks, cycle=cycle),
        cookies, mbtc, timeout,
    )
    if not data:
        raise QuickSightError("QuickSight no devolvio datos del dashboard BAT.")

    rows: list[dict[str, Any]] = []
    for item in data.get("data", []):
        group = item.get("group", [])
        if len(group) < 2:
            continue
        metric = str(group[0]).strip()
        date = str(group[1])[:10]
        if not metric or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            continue
        values = _agg_values((item.get("current") or {}).get("metrics", {}).get(COL_VALUE))
        if not values:
            continue
        rows.append({"metric": metric, "date": date, "values": values})
    return rows


__all__ = [
    "DASHBOARD_ID",
    "DEFAULT_CYCLE",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_week",
    "nostradamus_login_status",
]
