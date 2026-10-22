"""Amazon QuickSight — "Bags retornadas" (BTS Bags) dashboard integration.

Reproduces the dashboard's "Compliance % Trend View" numbers automatically
(no CSV download) by streaming the SAME aggregated query the dashboard runs
over the QuickSight WebSocket, using the operator's Midway/Firefox session.

Dashboard::

    https://us-east-1.quicksight.aws.amazon.com/sn/account/amzlbiaquicksight
        /dashboards/53d30af8-.../sheets/53d30af8-..._293c2ef4-...

Instead of pulling raw per-bag rows (which hit QuickSight's ~2048 row cap and
can't reproduce the layered metrics), we group by ``delivery_station_code`` +
``provider_company_short_code`` and request the dashboard's own calculated
fields as metrics. Their exact expressions (taken from the dashboard
definition) only depend on per-bag columns and three helper calc fields, with
**no parameters**, so they are self-contained:

* RTS Scan (5d0299e6)      = ``bag == return_process_bag``
* Recovered Bag (420076fd) = ``bag == bag_sorted OR bag == bag_open``
* Recovered DS (e67e12d1)  = ``open_time<=sort_date | sort_date null ?
                               open_station : sort_station``

Metrics returned per DSP (summed over the selected weeks):

* Dispatched Bags          (f00d3cde) -> "Salieron"
* Recovered Bags at Same DS(46764fc3) -> "Volvieron"
* Recovered Bags at other DS(bcfb4916)-> "Otra DS"
* Damaged Bags             (bb9e4c8f) -> "Dañadas"
* Unrecovered Bags         (3717137d) -> "No devueltas"
* %Compliance is recomputed as (Same + other) / Dispatched.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import time
import uuid
from typing import Any

import requests

try:  # websocket-client
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None

from app.integrations.firefox_cookies import (
    QUICKSIGHT_HOST_PATTERNS,
    read_cookies_from_profile,
)
from app.integrations.quicksight_client import (
    _extract_set_cookies,
    _metric_value,
    _parse_handshake_cookies,
)

QUICKSIGHT_REGION = "us-east-1"
QUICKSIGHT_HOST = f"{QUICKSIGHT_REGION}.quicksight.aws.amazon.com"
QUICKSIGHT_ALIAS = "amzlbiaquicksight"

DASHBOARD_ID = "53d30af8-e730-4034-9225-49259ce07687"
SHEET_ID = "53d30af8-e730-4034-9225-49259ce07687_293c2ef4-ea1b-4ad1-9b42-585a35c08d33"
STREAM_SOURCE_ID = "93366e4c-3b63-45c5-a5eb-c4641ed25851"
DATASET_PREFIX = "87a91b35-0b4e-422e-b407-79c9bfaf96a9"
# Any TABLE/PIVOT visual bound to the bags dataset works; QuickSight honours
# the group + metrics we send. This is the "List of not returned bags" pivot.
DEFAULT_VISUAL_ID = "53d30af8-e730-4034-9225-49259ce07687_dc07f88f-e6ef-43e6-b5b1-de08e28ac1c4"

STATION_COL = f"{DATASET_PREFIX}.delivery_station_code"
DSP_COL = f"{DATASET_PREFIX}.provider_company_short_code"
WEEK_COL = f"{DATASET_PREFIX}.year_week"
# The dashboard's weekly view keeps only the latest dispatch date per bag
# (see the "Dispatched Bags" note). That dedup is a filter on this flag; its
# truthy value isn't known offline, so ``fetch_bags_rows`` tries these in
# order and uses the first that returns rows (falling back to no dedup).
LATEST_COL = f"{DATASET_PREFIX}.latest_in_week"

# Metric calc-field ids (from the dashboard definition) -> internal key.
METRIC_IDS = {
    "salieron": "f00d3cde-aca4-4088-b989-627b80261212",   # Dispatched Bags
    "devueltas": "46764fc3-ef9e-4870-bf08-157b2fd99101",  # Recovered Bags at Same DS
    "otra_ds": "bcfb4916-3515-4074-91fd-334a8f26f9a2",    # Recovered Bags at other DS
    "danadas": "bb9e4c8f-b818-4b5c-b924-979469bbdd81",    # Damaged Bags
    "no_devueltas": "3717137d-a6fd-453c-a35d-2baf8e1b59a1",  # Unrecovered Bags
}

# Calc-field definitions (alias -> expression) sent in cfg.calculatedFields.
# Expressions are copied verbatim from the dashboard; helper fields first.
_P = DATASET_PREFIX
_RTS_SCAN = "5d0299e6-29c8-4b57-aeb1-1805b3c22203"
_RECOVERED = "420076fd-eb0a-4f58-90e7-9313bf6bc4fc"
_RECOVERED_DS = "e67e12d1-2e3e-481a-bcbe-355db7c31097"

CALCULATED_FIELDS = [
    {"alias": _RTS_SCAN, "func": f"ifelse({{{_P}.bag}}={{{_P}.return_process_bag}},1,0)"},
    {"alias": _RECOVERED,
     "func": f"ifelse({{{_P}.bag}}={{{_P}.bag_sorted}} or {{{_P}.bag}}={{{_P}.bag_open}},1,0)"},
    {"alias": _RECOVERED_DS,
     "func": (f"ifelse({{{_P}.open_time}}<={{{_P}.sort_date}} or isNull({{{_P}.sort_date}}),"
              f"{{{_P}.open_station}},{{{_P}.sort_station}})")},
    {"alias": METRIC_IDS["salieron"],
     "func": f"distinct_count(concat({{{_P}.bag}},toString({{{_P}.date}})))"},
    {"alias": METRIC_IDS["devueltas"],
     "func": (f"distinct_count(ifelse({{{_RECOVERED}}}=1 AND {{{_P}.delivery_station_code}}={{{_RECOVERED_DS}}} "
              f"and {{{_RTS_SCAN}}}=0 ,concat({{{_P}.bag}},tostring({{{_P}.date}})),null))")},
    {"alias": METRIC_IDS["otra_ds"],
     "func": (f"distinct_count(ifelse({{{_RECOVERED}}}=1 AND {{{_P}.delivery_station_code}}<>{{{_RECOVERED_DS}}} "
              f"and {{{_RECOVERED_DS}}}<>'' AND {{{_RTS_SCAN}}}=0 ,concat({{{_P}.bag}},tostring({{{_P}.date}})),null))")},
    {"alias": METRIC_IDS["danadas"],
     "func": (f"distinct_count(ifelse({{{_RECOVERED}}}=0 AND {{{_P}.bag}}={{{_P}.damage_label}} "
              f"AND {{{_RTS_SCAN}}}=0,{{{_P}.bag}},null))")},
    {"alias": METRIC_IDS["no_devueltas"],
     "func": (f"distinct_count(ifelse({{{_RTS_SCAN}}}=0 and {{{_RECOVERED}}}=0 "
              f"and {{{_P}.bag}}<>{{{_P}.damage_label}},concat({{{_P}.bag}},tostring({{{_P}.date}})),null))")},
]

LOGIN_URL = (
    f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}"
    f"/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"
)
_API_BASE = f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"


class QuickSightAuthError(RuntimeError):
    """No usable QuickSight session cookies / session expired."""


class QuickSightError(RuntimeError):
    """QuickSight returned an error frame or no data."""


def _read_cookies() -> dict[str, str]:
    try:
        raw = read_cookies_from_profile(
            QUICKSIGHT_HOST_PATTERNS, target_host=QUICKSIGHT_HOST, include_expired=True
        )
        return {k: v.strip('"') for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    try:
        resp = requests.get(
            LOGIN_URL,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            cookies=cookies, timeout=timeout, allow_redirects=False,
        )
        if resp.status_code == 200:
            m = re.search(r'"csrf"\s*:\s*"([^"]+)"', _html.unescape(resp.text))
            if m:
                return m.group(1)
    except requests.RequestException:
        pass
    return os.environ.get("QUICKSIGHT_MBTC", "").strip()


def _api_headers(mbtc: str) -> dict[str, str]:
    return {
        "User-Agent": _UA, "Accept": "*/*", "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc, "x-amzn-target": "api", "Referer": LOGIN_URL,
    }


def bags_login_status() -> dict[str, Any]:
    """Whether the QuickSight session can reach the bags dashboard."""
    cookies = _read_cookies()
    if not cookies:
        return {"ready": False, "cookie_count": 0, "host": QUICKSIGHT_HOST,
                "reason": "no_cookies", "login_url": LOGIN_URL}
    mbtc = _fetch_mbtc(cookies)
    url = (f"{_API_BASE}/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=1"
           f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}")
    try:
        resp = requests.get(url, headers=_api_headers(mbtc), cookies=cookies,
                            timeout=12, allow_redirects=False)
        body = resp.text.lower()
        live = resp.status_code == 200 and "authenticate" not in body and "error" not in body[:50]
        return {"ready": live, "cookie_count": len(cookies), "host": QUICKSIGHT_HOST,
                "reason": "" if live else "session_expired", "login_url": LOGIN_URL}
    except requests.RequestException:
        return {"ready": False, "cookie_count": len(cookies), "host": QUICKSIGHT_HOST,
                "reason": "network_error", "login_url": LOGIN_URL}


def _visual_id() -> str:
    return os.environ.get("QUICKSIGHT_BAGS_VISUAL_ID", "").strip() or DEFAULT_VISUAL_ID


def _stream_source_id() -> str:
    return os.environ.get("QUICKSIGHT_BAGS_STREAM_SOURCE_ID", "").strip() or STREAM_SOURCE_ID


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    url = (f"{_API_BASE}/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=175"
           f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}")
    merged = dict(cookies)
    try:
        resp = requests.get(url, headers=_api_headers(mbtc), cookies=merged,
                            timeout=timeout, allow_redirects=False)
        merged.update(_extract_set_cookies(resp))
        if resp.status_code in (401, 403):
            resp2 = requests.get(url, headers=_api_headers(mbtc), cookies=merged,
                                 timeout=timeout, allow_redirects=False)
            merged.update(_extract_set_cookies(resp2))
    except requests.RequestException:
        pass
    return merged


def _filter_in(col_id: str, values: list[str]) -> str:
    parts = []
    for v in values:
        raw = str(v).strip()
        if not raw:
            continue
        parts.append(raw if re.fullmatch(r"-?\d+", raw) else f'"{raw}"')
    return f"((in({{{col_id}}},[{','.join(parts)}],true)))"


def _build_start_vis(*, station: str, weeks: list[str], latest_value: str | None) -> dict[str, Any]:
    # Group by station + DSP + week so the caller can break results down per
    # week (and still aggregate per DSP).
    group_cols = [STATION_COL, DSP_COL, WEEK_COL]
    last = len(group_cols) - 1
    fields = []
    for i, cid in enumerate(group_cols):
        if i == last:
            fields.append({"name": cid, "limit": 5000,
                           "sort": {"name": DSP_COL, "dir": "ASC"},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    metrics = [{"name": mid, "func": "CUSTOM", "chartContext": mid} for mid in METRIC_IDS.values()]
    metrics.append({"name": "!calc.count.0.0", "func": "CUSTOM"})

    calc_fields = list(CALCULATED_FIELDS)
    calc_fields.append({"alias": "!calc.count.0.0", "func": "countOver(1)"})

    filters = [_filter_in(STATION_COL, [station])]
    week_values = [str(w).strip() for w in (weeks or []) if str(w).strip()]
    if week_values:
        filters.append(_filter_in(WEEK_COL, week_values))
    if latest_value:
        filters.append(_filter_in(LATEST_COL, [latest_value]))

    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metrics},
                "clsSupport": True,
                "calculatedFields": calc_fields,
                "filterExpressions": filters,
                "parameters": {},
            },
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Berlin",
            "streamSourceId": _stream_source_id(), "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": SHEET_ID, "visualId": _visual_id(), "visualArn": None,
            "visualType": "TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _connect_ws(cookies: dict[str, str], mbtc: str, timeout: float):
    ws_url = f"wss://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/websocket/?mbtc={mbtc}"
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            return websocket.create_connection(  # type: ignore[union-attr]
                ws_url, header=[f"User-Agent: {_UA}", f"Origin: https://{QUICKSIGHT_HOST}"],
                cookie=cookie_header, timeout=timeout, suppress_origin=True,
            ), cookies
        except Exception as exc:  # noqa: BLE001
            s = str(exc)
            if "(re)authenticate" in s or "return to https://console" in s:
                raise QuickSightAuthError(
                    "Sesión de AWS expirada. Reabre el dashboard en Firefox y reintenta.")
            if attempt == 0 and "403" in s:
                new = _parse_handshake_cookies(exc)
                if new:
                    cookies = {**cookies, **new}
                    continue
            if attempt == 0 and ("302" in s or "url is invalid" in s.lower()):
                raise QuickSightAuthError(
                    "Sesión de QuickSight no activa en la cuenta correcta. Abre el dashboard en Firefox.")
            raise QuickSightAuthError(f"No se pudo conectar al WebSocket de QuickSight: {s[:200]}")
    raise QuickSightAuthError("No se pudo conectar al WebSocket de QuickSight.")


def _run_query(*, station: str, weeks: list[str], latest_value: str | None,
               cookies: dict[str, str], mbtc: str, timeout: float) -> list[dict[str, Any]]:
    """Run one aggregated query; return per (dsp, week) metric rows."""
    start_vis = _build_start_vis(station=station, weeks=weeks, latest_value=latest_value)
    cid = start_vis["cid"]
    ws, _ = _connect_ws(cookies, mbtc, timeout)
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
            if "error" in msg:
                raise QuickSightError(f"QuickSight error: {msg.get('error')} - {msg.get('details')}")
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
        raise QuickSightError("QuickSight no devolvió datos de bags para ese periodo.")

    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        g = item.get("group", []) or []
        mcells = (item.get("current") or {}).get("metrics", {})
        dsp = str(g[1]).strip().upper() if len(g) > 1 and g[1] is not None else ""
        week = str(g[2]).strip() if len(g) > 2 and g[2] is not None else ""
        rec: dict[str, Any] = {"dsp": dsp, "week": week}
        for key, mid in METRIC_IDS.items():
            rec[key] = int(round(_metric_value(mcells.get(mid)) or 0))
        rows.append(rec)
    return rows


def _latest_candidates() -> list[str | None]:
    """Values to try for the ``latest_in_week`` dedup filter.

    Env ``QUICKSIGHT_BAGS_LATEST_VALUE`` pins one value ("off"/"none"
    disables the filter). Otherwise we cascade Y -> 1 -> no-filter and use
    the first that returns rows.
    """
    env = os.environ.get("QUICKSIGHT_BAGS_LATEST_VALUE", "").strip()
    if env:
        if env.lower() in ("off", "none", "false", "0-off"):
            return [None]
        return [env]
    return ["Y", "1", None]


def fetch_bags_rows(*, station: str, weeks: list[str] | str, timeout: float = 45.0) -> list[dict[str, Any]]:
    """Return per (dsp, week) metric rows for a station + one or more weeks.

    Each row: ``{dsp, week, salieron, devueltas, otra_ds, danadas, no_devueltas}``.
    Applies the dashboard's per-week "latest dispatch date" dedup filter,
    trying candidate flag values until one returns rows.
    """
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")
    station = str(station or "").strip().upper()
    if isinstance(weeks, str):
        weeks = [weeks]
    week_list = [str(w).strip() for w in (weeks or []) if str(w).strip()]

    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesión en {LOGIN_URL} en Firefox.")
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    cookies = _warm_up(cookies, mbtc, timeout)

    last_rows: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for latest_value in _latest_candidates():
        try:
            rows = _run_query(station=station, weeks=week_list, latest_value=latest_value,
                              cookies=cookies, mbtc=mbtc, timeout=timeout)
        except QuickSightAuthError:
            raise
        except QuickSightError as exc:
            # This dedup value / filter isn't accepted by the dataset (e.g. a
            # type mismatch on latest_in_week). Remember it and try the next
            # candidate; "no filter" is always the final, safe fallback.
            last_error = exc
            continue
        # A non-empty result means this dedup value is valid for the dataset.
        if any(r.get("salieron") for r in rows):
            return rows
        last_rows = rows or last_rows
    if not last_rows and last_error is not None:
        raise last_error
    return last_rows


def aggregate_by_dsp(rows: list[dict[str, Any]], allowed_dsps: list[str] | None = None) -> list[dict[str, Any]]:
    """Sum the per-DSP metric rows, keeping only DSPs configured in ALLOWED_DSPS.

    Every configured DSP always appears (even with zeros); providers not in
    config are dropped. ``%Compliance`` is recomputed as
    ``(devueltas + otra_ds) / salieron``.
    """
    allowed = [str(d).strip().upper() for d in (allowed_dsps or []) if str(d).strip()]
    allowed_set = set(allowed)
    by: dict[str, dict[str, Any]] = {}

    def ensure(dsp: str) -> dict[str, Any]:
        if dsp not in by:
            by[dsp] = {"dsp": dsp, "salieron": 0, "devueltas": 0, "otra_ds": 0,
                       "danadas": 0, "no_devueltas": 0, "compliance": 0.0}
        return by[dsp]

    for dsp in allowed:
        ensure(dsp)
    for r in rows:
        dsp = str(r.get("dsp") or "").strip().upper()
        if not dsp:
            continue
        if allowed_set and dsp not in allowed_set:
            continue
        agg = ensure(dsp)
        for key in ("salieron", "devueltas", "otra_ds", "danadas", "no_devueltas"):
            agg[key] += int(r.get(key) or 0)

    out = list(by.values())
    for agg in out:
        s = agg["salieron"]
        agg["compliance"] = round((agg["devueltas"] + agg["otra_ds"]) / s * 100.0, 1) if s else 0.0
    out.sort(key=lambda a: a["salieron"], reverse=True)
    return out


def build_by_week(rows: list[dict[str, Any]], allowed_dsps: list[str] | None = None) -> list[dict[str, Any]]:
    """Per (DSP, week) breakdown, keeping only configured DSPs.

    Each entry: ``{dsp, week, salieron, devueltas, otra_ds, danadas,
    no_devueltas, compliance}``, sorted by DSP then week.
    """
    allowed_set = {str(d).strip().upper() for d in (allowed_dsps or []) if str(d).strip()}
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        dsp = str(r.get("dsp") or "").strip().upper()
        week = str(r.get("week") or "").strip()
        if not dsp or not week:
            continue
        if allowed_set and dsp not in allowed_set:
            continue
        key = (dsp, week)
        cur = agg.setdefault(key, {"dsp": dsp, "week": week, "salieron": 0, "devueltas": 0,
                                   "otra_ds": 0, "danadas": 0, "no_devueltas": 0, "compliance": 0.0})
        for m in ("salieron", "devueltas", "otra_ds", "danadas", "no_devueltas"):
            cur[m] += int(r.get(m) or 0)
    out = list(agg.values())
    for cur in out:
        s = cur["salieron"]
        cur["compliance"] = round((cur["devueltas"] + cur["otra_ds"]) / s * 100.0, 1) if s else 0.0
    out.sort(key=lambda a: (a["dsp"], a["week"]))
    return out


def summarize_totals(per_dsp: list[dict[str, Any]]) -> dict[str, Any]:
    tot = {k: sum(a[k] for a in per_dsp) for k in
           ("salieron", "devueltas", "otra_ds", "danadas", "no_devueltas")}
    tot["compliance"] = (round((tot["devueltas"] + tot["otra_ds"]) / tot["salieron"] * 100.0, 1)
                         if tot["salieron"] else 0.0)
    return tot


__all__ = [
    "LOGIN_URL", "QuickSightAuthError", "QuickSightError",
    "aggregate_by_dsp", "bags_login_status", "build_by_week",
    "fetch_bags_rows", "summarize_totals",
]
