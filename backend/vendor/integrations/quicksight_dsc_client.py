"""Amazon QuickSight — DSC Detail View dashboard integration.

Reuses the operator's Midway/QuickSight Firefox session (same approach as
:mod:`app.integrations.quicksight_client` / ``quicksight_scheduling_client``)
to talk to the **DSC Detail View** dashboard:

    https://us-east-1.quicksight.aws.amazon.com/sn/account/amzlbiaquicksight
        /dashboards/cf1670d6-.../sheets/cf1670d6-..._770943c1-...

The dashboard's "DSC Detail View" sheet exposes a table visual filtered by
``station`` and ``week``. Its data (including ``dsc_bucket_list`` and the
distance column ``geo_dist``) streams over the QuickSight WebSocket, exactly
like the DPR dashboard. The constants below were taken from the operator's
HAR capture:

* ``DASHBOARD_ID``      — cf1670d6-d3b1-4c97-954d-c644bb0d9a66
* ``SHEET_ID``          — the DSC Detail View sheet
* ``STREAM_SOURCE_ID``  — prepared-data-source id (d85e6628-...)
* ``DATASET_PREFIX``    — column-id prefix; columns are ``<prefix>.<name>``

This module currently implements the **login/session** half (status check +
login URL) used by the frontend "Conectar QuickSight" button. The row fetch
(``fetch_dsc_detail``) is wired but needs the table visual id + column ids,
which are discovered at runtime from ``GetSheetForDashboard``.
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
    _parse_handshake_cookies,
)

QUICKSIGHT_REGION = "us-east-1"
QUICKSIGHT_HOST = f"{QUICKSIGHT_REGION}.quicksight.aws.amazon.com"
QUICKSIGHT_ALIAS = "amzlbiaquicksight"

DASHBOARD_ID = "cf1670d6-d3b1-4c97-954d-c644bb0d9a66"
SHEET_ID = "cf1670d6-d3b1-4c97-954d-c644bb0d9a66_770943c1-9eb9-47f2-9622-d3d6ac8a68c7"
STREAM_SOURCE_ID = "d85e6628-cdd9-464e-8be7-5d698544a9a4"
# Columns on this dashboard are addressed as ``<DATASET_PREFIX>.<name>``.
DATASET_PREFIX = "3f7992d8-9d44-4524-bd8e-b4aa7da2f7a1"

LOGIN_URL = (
    f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}"
    f"/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)


class QuickSightAuthError(RuntimeError):
    """No usable QuickSight session cookies / session expired."""


class QuickSightError(RuntimeError):
    """QuickSight returned an error frame or no data."""


def _read_cookies() -> dict[str, str]:
    try:
        raw = read_cookies_from_profile(
            QUICKSIGHT_HOST_PATTERNS,
            target_host=QUICKSIGHT_HOST,
            include_expired=True,
        )
        return {k: v.strip('"') for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _mbtc_default() -> str:
    return os.environ.get("QUICKSIGHT_MBTC", "").strip()


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Read the live per-session CSRF token (mbtc) from the dashboard page."""
    try:
        resp = requests.get(
            LOGIN_URL,
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


def dsc_login_status() -> dict[str, Any]:
    """Return whether the QuickSight session can reach the DSC dashboard.

    Mirrors ``quicksight_client.quicksight_login_status``: a real server
    call decides readiness. ``ready=True`` only when QuickSight accepts our
    cookies for this dashboard's sheet.
    """
    cookies = _read_cookies()
    if not cookies:
        return {
            "ready": False,
            "cookie_count": 0,
            "host": QUICKSIGHT_HOST,
            "reason": "no_cookies",
            "login_url": LOGIN_URL,
        }

    mbtc = _fetch_mbtc(cookies)
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=1"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": LOGIN_URL,
    }
    try:
        resp = requests.get(
            url, headers=headers, cookies=cookies, timeout=12, allow_redirects=False
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
            "host": QUICKSIGHT_HOST,
            "reason": "" if live else "session_expired",
            "login_url": LOGIN_URL,
        }
    except requests.RequestException:
        return {
            "ready": False,
            "cookie_count": len(cookies),
            "host": QUICKSIGHT_HOST,
            "reason": "network_error",
            "login_url": LOGIN_URL,
        }


# ---------------------------------------------------------------------------
# Data extraction (DSC Detail View table → rows)
# ---------------------------------------------------------------------------

_API_BASE = f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
_COL_RE = re.compile(re.escape(DATASET_PREFIX) + r"\.[A-Za-z0-9_]+")

# Optional manual overrides (set if the auto-discovery picks the wrong visual).
def _visual_id_override() -> str:
    return os.environ.get("QUICKSIGHT_DSC_VISUAL_ID", "").strip()


def _stream_source_id() -> str:
    return os.environ.get("QUICKSIGHT_DSC_STREAM_SOURCE_ID", "").strip() or STREAM_SOURCE_ID


def _api_headers(mbtc: str) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "*/*",
        "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": LOGIN_URL,
    }


def _get_sheet_definition(cookies: dict[str, str], mbtc: str, timeout: float = 20.0) -> dict[str, Any]:
    """Fetch the sheet definition (visuals + columns) via GetSheetForDashboard."""
    url = (
        f"{_API_BASE}/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=200"
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


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _collect_col_ids(node: Any) -> list[str]:
    """Recursively gather ``<prefix>.<name>`` column ids in document order."""
    found: list[str] = []

    def visit(obj: Any) -> None:
        if isinstance(obj, str):
            for m in _COL_RE.findall(obj):
                found.append(m)
        elif isinstance(obj, dict):
            for v in obj.values():
                visit(v)
        elif isinstance(obj, list):
            for v in obj:
                visit(v)

    visit(node)
    return _ordered_unique(found)


def _discover_table_visual(defn: dict[str, Any]) -> tuple[str, list[str]]:
    """Best-effort: find the detail TABLE visual id + its column ids.

    Walks the GetSheetForDashboard JSON, collecting dicts that carry a
    ``visualId`` and look like a table, and returns the one referencing the
    most columns (the DSC Detail View detail table).
    """
    candidates: list[tuple[str, str, list[str]]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            vid = (
                node.get("visualId")
                or node.get("VisualId")
                or node.get("visualID")
            )
            vtype = str(
                node.get("visualType") or node.get("type") or node.get("VisualType") or ""
            ).upper()
            if isinstance(vid, str) and vid:
                cols = _collect_col_ids(node)
                candidates.append((vid, vtype, cols))
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(defn)
    if not candidates:
        return "", []
    # Prefer TABLE / PIVOT visuals; among those, the one with most columns.
    table_like = [c for c in candidates if "TABLE" in c[1]] or candidates
    table_like.sort(key=lambda c: len(c[2]), reverse=True)
    best = table_like[0]
    return best[0], best[2]


def _col_name(col_id: str) -> str:
    return col_id.split(".", 1)[1] if "." in col_id else col_id


def _filter_in(col_id: str, value: str) -> str:
    raw = str(value).strip()
    if re.fullmatch(r"-?\d+", raw):
        payload = f"[{raw}]"
    else:
        payload = f'["{raw}"]'
    return f"((in({{{col_id}}},{payload},true)))"


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    """Trigger QuickSight's session bootstrap and capture Set-Cookie tokens."""
    url = (
        f"{_API_BASE}/sheets/{SHEET_ID}?Operation=GetSheetForDashboard&pageSize=175"
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


def _build_start_vis_dsc(
    visual_id: str,
    group_col_ids: list[str],
    *,
    station: str,
    week: str,
    year: str | None,
) -> dict[str, Any]:
    last = len(group_col_ids) - 1
    fields = []
    for i, cid in enumerate(group_col_ids):
        if i == last:
            fields.append(
                {
                    "name": cid,
                    "limit": 50000,
                    "sort": {"name": group_col_ids[0], "dir": "ASC"},
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
    metrics = [{"name": "!calc.count.0.0", "func": "CUSTOM"}]
    filters = [_filter_in(f"{DATASET_PREFIX}.station", station)]
    if week:
        filters.append(_filter_in(f"{DATASET_PREFIX}.week", week))
    if year:
        filters.append(_filter_in(f"{DATASET_PREFIX}.year", year))
    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
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
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": "Europe/Berlin",
            "streamSourceId": _stream_source_id(),
            "datasetVersionAlias": "$STABLE",
            "enableComputations": False,
            "computationsOnly": False,
            "sheetId": SHEET_ID,
            "visualId": visual_id,
            "visualArn": None,
            "visualType": "TABLE",
            "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()),
            "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _connect_ws(cookies: dict[str, str], mbtc: str, timeout: float):
    ws_url = f"wss://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/websocket/?mbtc={mbtc}"
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            return websocket.create_connection(  # type: ignore[union-attr]
                ws_url,
                header=[f"User-Agent: {_UA}", f"Origin: https://{QUICKSIGHT_HOST}"],
                cookie=cookie_header,
                timeout=timeout,
                suppress_origin=True,
            ), cookies
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


def fetch_dsc_detail(
    *,
    station: str,
    week: str,
    year: str | int | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Pull the DSC Detail View rows for a station + week.

    Returns a list of row dicts keyed by the dataset column names (so the
    caller gets ``tracking_id`` / ``dsc_bucket_list`` / ``geo_dist`` / ``dsp``
    / ``station`` / ``week`` …, whatever the visual exposes).
    """
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")
    station = str(station or "").strip().upper()
    week = str(week or "").strip()
    year_s = str(year).strip() if year not in (None, "") else None

    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesión en {LOGIN_URL} en Firefox."
        )
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    cookies = _warm_up(cookies, mbtc, timeout)

    defn = _get_sheet_definition(cookies, mbtc, timeout=timeout)
    visual_id = _visual_id_override()
    columns: list[str] = []
    if defn:
        disc_vid, disc_cols = _discover_table_visual(defn)
        if not visual_id:
            visual_id = disc_vid
        columns = disc_cols
    if not visual_id:
        raise QuickSightError(
            "No se pudo descubrir el visual de la tabla DSC Detail View. "
            "Define QUICKSIGHT_DSC_VISUAL_ID con el id del visual."
        )
    if not columns:
        # Fall back to the known filter columns + the requested extras.
        names = [
            "year", "week", "country", "station", "dsp", "shipment_reason",
            "tracking_id", "dsc_bucket_list", "geo_dist", "dsc_bucket",
        ]
        columns = [f"{DATASET_PREFIX}.{n}" for n in names]

    # Guarantee the columns we cross-match / surface are always requested,
    # even if visual auto-discovery missed them. The join key is tracking_id.
    have = {_col_name(c) for c in columns}
    for name in ("tracking_id", "dsc_bucket_list", "geo_dist"):
        if name not in have:
            columns.append(f"{DATASET_PREFIX}.{name}")

    start_vis = _build_start_vis_dsc(
        visual_id, columns, station=station, week=week, year=year_s
    )
    cid = start_vis["cid"]
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
        raise QuickSightError("QuickSight no devolvió datos para la tabla DSC Detail View.")

    names = [_col_name(c) for c in columns]
    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        g = item.get("group", []) or []
        record: dict[str, Any] = {}
        for idx, name in enumerate(names):
            record[name] = g[idx] if idx < len(g) else None
        rows.append(record)
    return rows


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QUICKSIGHT_HOST",
    "QuickSightAuthError",
    "QuickSightError",
    "dsc_login_status",
    "fetch_dsc_detail",
]
