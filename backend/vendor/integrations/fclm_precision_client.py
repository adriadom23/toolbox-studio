"""FCLM Precision % — data sources for the Flow Meeting NS "FCLM" tab.

Two independent sources, joined by Labor Process:

1. **QuickSight** — "FCLM Precision Labor Process Raw Data" table on the
   Self-Assist dashboard (account ``amzlbiaquicksight``). Provides the
   *planned hours* per Labor Process for a station + labour day. These feed
   the "Available hours" row of the FCLM Precision % box (same numbers the
   operator used to paste into the FLOW-MEETING workbook's "Labor Process"
   sheet). Pulled over the QuickSight WebSocket with the operator's Firefox
   session, exactly like :mod:`app.integrations.quicksight_dsc_client`.

2. **FCLM portal** — the PPA "inspect node" page
   (``https://fclm-portal.amazon.com/ppa/inspect/node``) for an intraday
   window. Its process-path rollup table provides the *actual hours* per
   Labor Process Name ("Actual time FCLM" row). Plain Midway-authenticated
   HTML, parsed with BeautifulSoup.

The 12 columns of the box (``PROCESS_COLUMNS``) replicate the
"FCLM Precision %" block of the FLOW-MEETING workbook (tab "FLOW DOBLE PH",
E40:Q45): each column maps one QuickSight Labor Process (by FCLM process id)
to one or more FCLM portal process-path names.

All ids below were captured from the operator's HAR files (2026-08-13); env
overrides allow rotating them without a code change.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import time
import uuid
from datetime import date, datetime
from typing import Any

import requests

try:  # websocket-client
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None

from app.integrations.firefox_cookies import (
    FCLM_HOST_PATTERNS,
    QUICKSIGHT_HOST_PATTERNS,
    midway_cookies_from_profile,
    read_cookies_from_profile,
)
from app.integrations.quicksight_client import (
    _extract_set_cookies,
    _parse_handshake_cookies,
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)


# ---------------------------------------------------------------------------
# Box structure (mirrors the workbook's "FCLM Precision %" block)
# ---------------------------------------------------------------------------

# Each column: ``key`` (payload key), ``label`` (column header, kept as the
# workbook's short names), ``qs_process_ids`` (FCLM process ids whose
# QuickSight ``planned_hours`` sum into "Available hours") and ``ppa`` (how
# to match the FCLM portal "Labor Process Name": ``equals`` exact names
# and/or ``contains`` substrings, summed over "Actual Hours").
PROCESS_COLUMNS: tuple[dict[str, Any], ...] = (
    {"key": "problemSolve", "label": "Problem Solve",
     "qs_process_ids": ("100465",),
     "ppa": {"contains": ("Sort Problem Solve",)}},
    {"key": "ws", "label": "W/S (3hrs)",
     "qs_process_ids": ("100405",),
     "ppa": {"equals": ("Inbound Dock W/S",)}},
    {"key": "asmlF", "label": "ASML F (3hrs)",
     "qs_process_ids": ("100943",),
     "ppa": {"equals": ("ASML Induct Loader",)}},
    {"key": "labeler", "label": "Labeler (3hrs)",
     "qs_process_ids": ("100796",),
     "ppa": {"equals": ("Labeler",)}},
    {"key": "asmlP", "label": "ASML P (3hrs)",
     "qs_process_ids": ("100944",),
     "ppa": {"equals": ("ASML Pusher",)}},
    {"key": "straightener", "label": "Straigh (3hrs)",
     "qs_process_ids": ("100477",),
     "ppa": {"contains": ("Auto Divert Straightener",)}},
    {"key": "adtaS", "label": "ADTA S",
     "qs_process_ids": ("100541",),
     "ppa": {"contains": ("ADTA Container Building",)}},
    {"key": "cb", "label": "C/B (4.5hrs)",
     "qs_process_ids": ("100021",),
     "ppa": {"equals": ("Container Building",)}},
    {"key": "pToB", "label": "P to B (4.5hrs)",
     "qs_process_ids": ("100440",),
     "ppa": {"equals": ("Pick to Buffer",)}},
    {"key": "nonCon", "label": "Non Con M/H",
     "qs_process_ids": ("100570",),
     "ppa": {"contains": ("Non Con Manual Handling",)}},
    {"key": "diverter", "label": "D (1.5hrs)",
     "qs_process_ids": ("100460",),
     "ppa": {"equals": ("Diverter",)}},
    {"key": "fiveS", "label": "5S",
     "qs_process_ids": ("100470",),
     "ppa": {"contains": ("5S / Non Productive",)}},
)


# ---------------------------------------------------------------------------
# QuickSight — FCLM Precision Labor Process Raw Data (planned/available hours)
# ---------------------------------------------------------------------------

QUICKSIGHT_HOST = "us-east-1.quicksight.aws.amazon.com"
QUICKSIGHT_ALIAS = "amzlbiaquicksight"

QS_DASHBOARD_ID = os.environ.get(
    "FCLM_QS_DASHBOARD_ID", "04393eda-8384-4dcf-8d9d-935f897a0fa6"
)
QS_SHEET_ID = os.environ.get(
    "FCLM_QS_SHEET_ID",
    "04393eda-8384-4dcf-8d9d-935f897a0fa6_6a4e1885-d3c8-4a4d-b458-60c7966f9ba9",
)
QS_VISUAL_ID = os.environ.get(
    "FCLM_QS_VISUAL_ID",
    "04393eda-8384-4dcf-8d9d-935f897a0fa6_34c05e91-ec15-4cd5-8e64-2a9598caa57b",
)
# preparedDataSourceId of the "FCLM Precision Labor Process Raw Data" visual.
QS_STREAM_SOURCE_ID = os.environ.get(
    "FCLM_QS_STREAM_SOURCE_ID", "c2cef322-514b-4002-bbaf-5afe3de83f47"
)
# Dataset prefix of the visual's physical columns (``<prefix>.<name>``).
# NOTE: only PHYSICAL dataset columns can be requested over the WebSocket —
# the visual's calculated columns (Week / Process Group / Labor Process
# name) are rejected by the query service ("SERVICE" error frame), verified
# live on 2026-08-13. ``process_id`` + ``planned_hours`` cover everything
# the box needs.
QS_DATASET_PREFIX = os.environ.get(
    "FCLM_QS_DATASET_PREFIX", "0cc1ceef-3c01-4e69-9293-89e9382beb95"
)

QS_LOGIN_URL = (
    f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}"
    f"/dashboards/{QS_DASHBOARD_ID}/sheets/{QS_SHEET_ID}"
)

_QS_API_BASE = (
    f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}"
    f"/api/dashboards/{QS_DASHBOARD_ID}"
)


class FclmQsAuthError(RuntimeError):
    """No usable QuickSight session cookies / session expired."""


class FclmQsError(RuntimeError):
    """QuickSight returned an error frame or no data."""


def _qs_read_cookies() -> dict[str, str]:
    try:
        raw = read_cookies_from_profile(
            QUICKSIGHT_HOST_PATTERNS,
            target_host=QUICKSIGHT_HOST,
            include_expired=True,
        )
        return {k: v.strip('"') for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _qs_headers(mbtc: str) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "*/*",
        "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": QS_LOGIN_URL,
    }


def _qs_fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Read the live per-session CSRF token (mbtc) from the dashboard page."""
    try:
        resp = requests.get(
            QS_LOGIN_URL,
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
    return os.environ.get("QUICKSIGHT_MBTC", "").strip()


def _qs_warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    """Trigger QuickSight's session bootstrap and capture Set-Cookie tokens."""
    url = (
        f"{_QS_API_BASE}/sheets/{QS_SHEET_ID}?Operation=GetSheetForDashboard"
        f"&pageSize=175&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    merged = dict(cookies)
    try:
        resp = requests.get(
            url, headers=_qs_headers(mbtc), cookies=merged,
            timeout=timeout, allow_redirects=False,
        )
        merged.update(_extract_set_cookies(resp))
        if resp.status_code in (401, 403):
            resp2 = requests.get(
                url, headers=_qs_headers(mbtc), cookies=merged,
                timeout=timeout, allow_redirects=False,
            )
            merged.update(_extract_set_cookies(resp2))
    except requests.RequestException:
        pass
    return merged


def fclm_qs_login_status() -> dict[str, Any]:
    """Real server call deciding whether the QuickSight session is usable."""
    cookies = _qs_read_cookies()
    if not cookies:
        return {
            "ready": False, "cookie_count": 0, "host": QUICKSIGHT_HOST,
            "reason": "no_cookies", "login_url": QS_LOGIN_URL,
        }
    mbtc = _qs_fetch_mbtc(cookies)
    url = (
        f"{_QS_API_BASE}/sheets/{QS_SHEET_ID}?Operation=GetSheetForDashboard"
        f"&pageSize=1&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    try:
        resp = requests.get(
            url, headers=_qs_headers(mbtc), cookies=cookies,
            timeout=12, allow_redirects=False,
        )
        body = resp.text.lower()
        live = (
            resp.status_code == 200
            and "authenticate" not in body
            and "error" not in body[:50]
        )
        return {
            "ready": live, "cookie_count": len(cookies), "host": QUICKSIGHT_HOST,
            "reason": "" if live else "session_expired", "login_url": QS_LOGIN_URL,
        }
    except requests.RequestException:
        return {
            "ready": False, "cookie_count": len(cookies), "host": QUICKSIGHT_HOST,
            "reason": "network_error", "login_url": QS_LOGIN_URL,
        }


def _qs_connect_ws(cookies: dict[str, str], mbtc: str, timeout: float):
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
                raise FclmQsAuthError(
                    "Sesión de AWS expirada. Abre el dashboard de QuickSight en "
                    "Firefox para renovarla y reintenta."
                )
            if attempt == 0 and "403" in exc_str:
                new_cookies = _parse_handshake_cookies(exc)
                if new_cookies:
                    cookies = {**cookies, **new_cookies}
                    continue
            if attempt == 0 and ("302" in exc_str or "url is invalid" in exc_str.lower()):
                raise FclmQsAuthError(
                    "Sesión de QuickSight no activa en la cuenta correcta. "
                    "Abre el dashboard en Firefox."
                )
            raise FclmQsAuthError(
                f"No se pudo conectar al WebSocket de QuickSight: {exc_str[:200]}"
            )
    raise FclmQsAuthError("No se pudo conectar al WebSocket de QuickSight.")


def _qs_build_start_vis(station: str) -> dict[str, Any]:
    """START_VIS for the Labor Process Raw Data table, filtered by station.

    We request only PHYSICAL columns (calculated ones fail with a SERVICE
    error). The labour-day filter is applied client-side (the returned
    ``reporting_date`` is a ``YYYY-MM-DD HH:MM:SS.mmm`` string), the
    station one server-side on ``location_allocated``.
    """
    group_cols = [
        f"{QS_DATASET_PREFIX}.location_allocated",
        f"{QS_DATASET_PREFIX}.reporting_date",
        f"{QS_DATASET_PREFIX}.process_id",
        f"{QS_DATASET_PREFIX}.actual_hours",
        f"{QS_DATASET_PREFIX}.planned_hours",
    ]
    last = len(group_cols) - 1
    fields = []
    for i, cid in enumerate(group_cols):
        if i == last:
            fields.append({
                "name": cid, "limit": 50000,
                "sort": {"name": f"{QS_DATASET_PREFIX}.reporting_date", "dir": "DESC"},
                "chartContext": cid, "offset": 0, "partitioned": False,
            })
        else:
            fields.append({
                "name": cid, "limit": -1, "sort": None,
                "chartContext": cid, "offset": -1, "partitioned": False,
            })
    filters = [
        f'((in({{{QS_DATASET_PREFIX}.location_allocated}},["{station}"],true)))',
    ]
    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": QS_DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {
                    "fields": fields,
                    "metrics": [{"name": "!calc.count.0.0", "func": "CUSTOM"}],
                },
                "clsSupport": True,
                "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                "filterExpressions": filters,
                "parameters": {},
            },
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": "Europe/Berlin",
            "streamSourceId": QS_STREAM_SOURCE_ID,
            "datasetVersionAlias": "$STABLE",
            "enableComputations": False,
            "computationsOnly": False,
            "sheetId": QS_SHEET_ID,
            "visualId": QS_VISUAL_ID,
            "visualArn": None,
            "visualType": "TABLE",
            "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()),
            "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _same_day(raw: Any, day: date) -> bool:
    """True when a QuickSight ``reporting_date`` cell falls on ``day``.

    Tolerates epoch milliseconds/seconds and the usual string formats
    (``2026-08-13``, ``2026/08/13``, ISO timestamps).
    """
    if raw in (None, ""):
        return False
    s = str(raw).strip()
    if re.fullmatch(r"-?\d{12,14}", s):  # epoch ms
        try:
            return datetime.utcfromtimestamp(int(s) / 1000.0).date() == day
        except (ValueError, OverflowError, OSError):
            return False
    if re.fullmatch(r"-?\d{9,11}", s):  # epoch s
        try:
            return datetime.utcfromtimestamp(int(s)).date() == day
        except (ValueError, OverflowError, OSError):
            return False
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) == day
        except ValueError:
            return False
    return False


def fetch_qs_labor_process_rows(
    *, station: str, day: date, timeout: float = 30.0
) -> list[dict[str, Any]]:
    """Pull the FCLM Precision Labor Process Raw Data rows for station+day.

    Returns rows: ``{process_id, labor_process, actual_hours, planned_hours}``.
    """
    if websocket is None:
        raise FclmQsError("Falta la dependencia 'websocket-client'.")
    station = str(station or "").strip().upper()
    if not station:
        raise FclmQsError("Sin estación para la consulta de QuickSight.")

    cookies = _qs_read_cookies()
    if not cookies:
        raise FclmQsAuthError(
            f"No hay cookies de QuickSight. Inicia sesión en {QS_LOGIN_URL} en Firefox."
        )
    mbtc = _qs_fetch_mbtc(cookies, timeout=timeout)
    cookies = _qs_warm_up(cookies, mbtc, timeout)

    start_vis = _qs_build_start_vis(station)
    cid = start_vis["cid"]
    ws, cookies = _qs_connect_ws(cookies, mbtc, timeout)

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
                raise FclmQsError(
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
        raise FclmQsError(
            "QuickSight no devolvió datos de FCLM Precision Labor Process."
        )

    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        g = item.get("group", []) or []
        # Order mirrors _qs_build_start_vis group_cols.
        row = {
            "location": g[0] if len(g) > 0 else None,
            "reporting_date": g[1] if len(g) > 1 else None,
            "process_id": str(g[2]).strip() if len(g) > 2 and g[2] is not None else "",
            "labor_process": "",
            "actual_hours": _to_float(g[3] if len(g) > 3 else None),
            "planned_hours": _to_float(g[4] if len(g) > 4 else None),
        }
        # Belt & braces: the station filter is also enforced client-side.
        loc = str(row["location"] or "").strip().upper()
        if loc and loc != station:
            continue
        if not _same_day(row["reporting_date"], day):
            continue
        rows.append(row)
    return rows


def available_hours_by_column(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Sum QuickSight ``planned_hours`` into the 12 box columns (by id)."""
    by_id: dict[str, float] = {}
    for row in rows:
        pid = str(row.get("process_id") or "").strip()
        # The id may also ride inside "Name (100465)".
        if not pid:
            m = re.search(r"\((\d{5,7})\)", str(row.get("labor_process") or ""))
            pid = m.group(1) if m else ""
        hours = row.get("planned_hours")
        if pid and hours is not None:
            by_id[pid] = by_id.get(pid, 0.0) + float(hours)
    out: dict[str, float] = {}
    for col in PROCESS_COLUMNS:
        total = 0.0
        seen = False
        for pid in col["qs_process_ids"]:
            if pid in by_id:
                total += by_id[pid]
                seen = True
        if seen:
            out[col["key"]] = round(total, 4)
    return out


# ---------------------------------------------------------------------------
# FCLM portal — PPA inspect/node (actual hours per process path)
# ---------------------------------------------------------------------------

FCLM_HOST = "fclm-portal.amazon.com"
FCLM_LOGIN_URL = f"https://{FCLM_HOST}/"


class FclmPortalAuthError(RuntimeError):
    """No usable FCLM portal (Midway) session cookies / session expired."""


class FclmPortalError(RuntimeError):
    """The FCLM portal request or parsing failed."""


def _fclm_read_cookies() -> dict[str, str]:
    try:
        raw = read_cookies_from_profile(
            FCLM_HOST_PATTERNS,
            target_host=FCLM_HOST,
            include_expired=True,
        )
        return {k: v.strip('"') for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


# The portal's ``amzn_sso_token`` only lives ~30 minutes; the browser
# re-mints it transparently by following the Midway OIDC redirect chain.
# We do the same: a requests.Session seeded with the portal cookies PLUS the
# Midway cookies (host-scoped to ``midway-auth.amazon.com``) follows the
# 302 chain and lands authenticated (verified live 2026-08-28: an expired
# token + live Midway session re-mints on any portal GET).
def _fclm_session() -> requests.Session | None:
    portal = _fclm_read_cookies()
    try:
        midway = midway_cookies_from_profile()
    except Exception:  # noqa: BLE001
        midway = {}
    if not portal and not midway:
        return None
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
    })
    for k, v in portal.items():
        sess.cookies.set(k, v.strip('"'), domain=FCLM_HOST)
    for k, v in midway.items():
        sess.cookies.set(k, v.strip('"'), domain="midway-auth.amazon.com")
    return sess


def _fclm_sso_authenticated(sess: requests.Session, timeout: float = 12.0) -> bool:
    """True when the portal's own ``/sso/login`` says the session is live."""
    try:
        resp = sess.get(
            f"https://{FCLM_HOST}/sso/login",
            headers={"Accept": "application/json"},
            timeout=timeout, allow_redirects=False,
        )
        return resp.status_code == 200 and bool(resp.json().get("is_authenticated"))
    except (requests.RequestException, ValueError):
        return False


# Cached live session (module level) so the identify poll and multi-window
# fetches don't repeat the re-mint chain on every call. The fetch path
# self-heals anyway (Midway cookies ride in the jar, so a token expiring
# mid-batch re-mints on the next redirect).
_FCLM_SESSION_TTL = 15 * 60.0
_fclm_session_cache: dict[str, Any] = {"sess": None, "at": 0.0}


def _fclm_live_session(timeout: float = 30.0) -> requests.Session | None:
    """A portal session ready to use, re-minting the token when expired."""
    now = time.time()
    cached = _fclm_session_cache.get("sess")
    if cached is not None and now - float(_fclm_session_cache.get("at") or 0) < _FCLM_SESSION_TTL:
        return cached
    sess = _fclm_session()
    if sess is None:
        return None
    if not _fclm_sso_authenticated(sess):
        # Expired portal token: mint by following the OIDC chain on the
        # (light, ~180 KB) portal root page with the Midway cookies.
        try:
            sess.get(f"https://{FCLM_HOST}/", timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            return None
        if not _fclm_sso_authenticated(sess):
            return None
    _fclm_session_cache["sess"] = sess
    _fclm_session_cache["at"] = now
    return sess


def _fclm_ppa_url(station: str, start: datetime, end: datetime) -> str:
    """Build the PPA inspect-node URL for an intraday window (same shape as
    the operator's browser URL captured in the HAR)."""
    day_fmt = "%Y/%m/%d"
    params = {
        "nodeType": "DS",
        "warehouseId": station,
        "spanType": "Intraday",
        "startDateIntraday": start.strftime(day_fmt),
        "startHourIntraday": str(start.hour),
        "startMinuteIntraday": str(start.minute),
        "endDateIntraday": end.strftime(day_fmt),
        "endHourIntraday": str(end.hour),
        "endMinuteIntraday": str(end.minute),
        # The portal expects these companions even in Intraday mode.
        "maxIntradayDays": "1",
        "startDateDay": start.strftime(day_fmt),
        "startDateWeek": start.strftime(day_fmt),
        "startDateMonth": start.strftime("%Y/%m/01"),
    }
    query = "&".join(f"{k}={requests.utils.quote(v, safe='')}" for k, v in params.items())
    return f"https://{FCLM_HOST}/ppa/inspect/node?{query}"


def fclm_portal_login_status() -> dict[str, Any]:
    """Session probe: portal ``/sso/login`` + silent Midway re-mint.

    The portal token only lives ~30 minutes, but with a live Midway session
    it re-mints transparently (like the browser does), so "expired token +
    live Midway" still counts as ready — the minted session stays cached
    for the actual fetches. Only a dead Midway session reports not ready.
    """
    cookies = _fclm_read_cookies()
    if not cookies:
        return {
            "ready": False, "cookie_count": 0, "host": FCLM_HOST,
            "reason": "no_cookies", "login_url": FCLM_LOGIN_URL,
        }
    try:
        sess = _fclm_live_session()
    except requests.RequestException:
        sess = None
    live = sess is not None
    return {
        "ready": live, "cookie_count": len(cookies), "host": FCLM_HOST,
        "reason": "" if live else "session_expired", "login_url": FCLM_LOGIN_URL,
    }


def _direct_table_rows(table) -> list:
    """The table's own ``tr`` elements (not those of nested tables)."""
    rows = []
    for section in table.find_all(["thead", "tbody", "tfoot"], recursive=False):
        rows.extend(section.find_all("tr", recursive=False))
    rows.extend(table.find_all("tr", recursive=False))
    return rows


def _expand_html_table(table) -> list[list[str]]:
    """Expand an HTML table into a text grid honouring row/colspans."""
    trs = _direct_table_rows(table)
    grid: list[list[str | None]] = [[] for _ in trs]

    def ensure(r: int, c: int) -> None:
        row = grid[r]
        while len(row) <= c:
            row.append(None)

    for r_idx, tr in enumerate(trs):
        cells = tr.find_all(["th", "td"], recursive=False)
        col = 0
        for cell in cells:
            ensure(r_idx, col)
            # Skip positions already filled by a rowspan from above.
            while grid[r_idx][col] is not None:
                col += 1
                ensure(r_idx, col)
            text = " ".join(cell.get_text(" ", strip=True).split())
            try:
                colspan = max(1, int(cell.get("colspan") or 1))
            except (TypeError, ValueError):
                colspan = 1
            try:
                rowspan = max(1, int(cell.get("rowspan") or 1))
            except (TypeError, ValueError):
                rowspan = 1
            for r_off in range(rowspan):
                r_target = r_idx + r_off
                if r_target >= len(grid):
                    break
                for c_off in range(colspan):
                    ensure(r_target, col + c_off)
                    grid[r_target][col + c_off] = text
            col += colspan
    return [
        [c if c is not None else "" for c in row]
        for row in grid
        if any(c not in (None, "") for c in row)
    ]


# One LaborProcess node of the JSON blob the PPA page embeds for its JS
# tables (the visible table of the operator's XPath is rendered client-side
# from this data, so the raw HTML carries no <tr id="labor-..."> rows).
# ``pipelineTotal.actualValues.hours`` is the process' total actual hours
# for the requested window. Verified live on 2026-08-13.
_PPA_PROCESS_RE = re.compile(
    r'\{"id":"(\d+)","name":"((?:[^"\\]|\\.)*)","typeName":"LaborProcess",'
    r'"pipelineTotal":\{"actualValues":\{"units":[-0-9.Ee]+,'
    r'"hours":([-0-9.Ee]+)'
)


def parse_ppa_embedded_rows(page_html: str) -> list[dict[str, Any]]:
    """Extract ``{id, name, actual_hours}`` from the embedded PPA JSON.

    Keeps the FIRST occurrence per process id (``pipelineTotal`` is already
    the rollup for the process; a second hit would be a duplicate copy of
    the blob, not extra hours).
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _PPA_PROCESS_RE.finditer(page_html):
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            name = json.loads(f'"{m.group(2)}"')
        except ValueError:
            name = m.group(2)
        hours = _to_float(m.group(3))
        if hours is None:
            continue
        rows.append({"id": pid, "name": str(name).strip(), "actual_hours": hours})
    return rows


def parse_ppa_process_rows(page_html: str) -> list[dict[str, Any]]:
    """Extract ``{name, actual_hours}`` rows from the PPA rollup table.

    Fallback path (server-rendered table variants): finds the table whose
    header row contains both "Labor Process Name" (any header with
    "Process" + "Name") and "Actual Hours".
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "html.parser")
    for table in soup.find_all("table"):
        grid = _expand_html_table(table)
        if not grid:
            continue
        header_idx = None
        name_col = hours_col = None
        for i, row in enumerate(grid[:8]):
            lower = [c.lower() for c in row]
            n_idx = h_idx = None
            for j, cell in enumerate(lower):
                if n_idx is None and "process" in cell and "name" in cell:
                    n_idx = j
                if h_idx is None and "actual" in cell and "hour" in cell:
                    h_idx = j
            if n_idx is not None and h_idx is not None and n_idx != h_idx:
                header_idx, name_col, hours_col = i, n_idx, h_idx
                break
        if header_idx is None:
            continue
        rows: list[dict[str, Any]] = []
        for row in grid[header_idx + 1:]:
            if name_col >= len(row) or hours_col >= len(row):
                continue
            name = row[name_col].strip()
            if not name or name.lower() in ("total", "totals"):
                continue
            hours = _to_float(row[hours_col])
            if hours is None:
                continue
            rows.append({"name": name, "actual_hours": hours})
        if rows:
            return rows
    return []


def fetch_ppa_page_html(
    *, station: str, start: datetime, end: datetime, timeout: float = 60.0,
    session: requests.Session | None = None,
) -> str:
    """Fetch the raw PPA inspect-node page HTML for an intraday window.

    ``session`` lets multi-window callers mint the portal session once and
    reuse it across requests. Redirects are followed: with a live Midway
    session an expired portal token re-mints in-flight (browser behaviour);
    only a chain that lands on Midway's login page counts as expired.
    """
    station = str(station or "").strip().upper()
    if not station:
        raise FclmPortalError("Sin estación para la consulta de FCLM.")
    sess = session or _fclm_live_session()
    if sess is None:
        raise FclmPortalAuthError(
            f"Sesión de FCLM no disponible. Inicia sesión en {FCLM_LOGIN_URL} en Firefox."
        )
    url = _fclm_ppa_url(station, start, end)
    try:
        resp = sess.get(url, timeout=timeout, allow_redirects=True)
    except requests.TooManyRedirects as exc:
        raise FclmPortalAuthError("Sesión de FCLM caducada (bucle de redirects).") from exc
    except requests.RequestException as exc:
        raise FclmPortalError(f"FCLM no respondió: {exc}") from exc
    final_url = str(resp.url or "").lower()
    if "midway" in final_url:
        raise FclmPortalAuthError("Sesión de Midway caducada (página de login).")
    if resp.status_code in (401, 403):
        raise FclmPortalAuthError(f"FCLM devolvió HTTP {resp.status_code}.")
    if resp.status_code != 200:
        raise FclmPortalError(f"FCLM devolvió HTTP {resp.status_code}.")
    body = resp.text
    if "midway" in body[:4000].lower() and "authenticat" in body[:4000].lower():
        raise FclmPortalAuthError("Sesión de FCLM caducada (página de Midway).")
    return body


def fetch_ppa_process_rows(
    *, station: str, start: datetime, end: datetime, timeout: float = 60.0
) -> list[dict[str, Any]]:
    """Fetch + parse the PPA process rows for an intraday window."""
    body = fetch_ppa_page_html(station=station, start=start, end=end, timeout=timeout)
    # Primary: the embedded JSON blob (process id + total actual hours).
    rows = parse_ppa_embedded_rows(body)
    if not rows:
        # Fallback: server-rendered table variants.
        rows = parse_ppa_process_rows(body)
    if not rows:
        raise FclmPortalError(
            "No se encontraron los Process Paths en la página de FCLM."
        )
    return rows


def actual_hours_by_column(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Sum FCLM PPA ``actual_hours`` into the 12 box columns.

    Rows from the embedded JSON carry the FCLM process ``id`` (the same ids
    QuickSight uses), so the primary match is by id; rows without an id
    (HTML-table fallback) match by name (exact / contains), replicating the
    workbook's SUMIFS criteria.
    """
    out: dict[str, float] = {}
    for col in PROCESS_COLUMNS:
        ids = set(col["qs_process_ids"])
        spec = col["ppa"]
        equals = {n.lower() for n in spec.get("equals", ())}
        contains = [n.lower() for n in spec.get("contains", ())]
        total = 0.0
        seen = False
        for row in rows:
            pid = str(row.get("id") or "").strip()
            if pid:
                match = pid in ids
            else:
                name = str(row.get("name") or "").strip().lower()
                if not name:
                    continue
                match = name in equals or any(sub in name for sub in contains)
            if match:
                total += float(row.get("actual_hours") or 0.0)
                seen = True
        if seen:
            out[col["key"]] = round(total, 4)
    return out


# ---------------------------------------------------------------------------
# Oversize units — Flow Meeting NS "Oversize · Hourly Real" row
# ---------------------------------------------------------------------------

# Box types that count as "Over" (user instruction 2026-08-28): the Actual
# Units of these sizes are summed across the Induct + ASML (+ ADTA when the
# station has it) labor processes of the PPA page.
OVERSIZE_BOX_TYPES: tuple[str, ...] = (
    "Non-Conveyable Plus",
    "Non-Conveyable",
    "Extra Large",
    "Large",
)

# LaborProcess ids whose per-size breakdown feeds the Over sum (same global
# FCLM ids QuickSight uses; verified live on DCZ4, 2026-08-28).
OVERSIZE_PROCESS_IDS: dict[str, str] = {
    "100020": "induct",  # Induct
    "100942": "asml",    # ASML Induct
    "100541": "adta",    # ADTA Container Building
}

# One aggregatedDataList entry of the embedded PPA JSON: per-process,
# per-attribute (work_flow / box_type) actual values. The visible size rows
# of the operator's XPath table are rendered client-side from these.
_PPA_AGG_ENTRY_RE = re.compile(
    r'"processAttributes":\{"processId":(\d+),"processName":"(?:[^"\\]|\\.)*",'
    r'"laborTrackingType":"[^"]*","attributes":\{([^{}]*)\},'
    r'"additionalAttributes":(?:null|"[^"]*"|\{[^{}]*\}),'
    r'"availability":[^,{}]*\},'
    r'"lineItemData":\{"actualValues":\{"units":([-0-9.Ee]+)'
)
_ATTR_WORKFLOW_RE = re.compile(r'"work_flow":"((?:[^"\\]|\\.)*)"')
_ATTR_BOX_TYPE_RE = re.compile(r'"box_type":"((?:[^"\\]|\\.)*)"')


def parse_ppa_oversize_units(page_html: str) -> dict[str, Any]:
    """Sum the Over Actual Units from a PPA page's embedded JSON.

    Adds up ``actualValues.units`` of the ``OVERSIZE_BOX_TYPES`` size rows
    for the Induct / ASML Induct / ADTA Container Building processes,
    SKIPPING the duplicated ``work_flow == "Master Session"`` copies (the
    user's explicit warning: "si pone Master Session, no es ese").

    Returns ``{found, units, by_process}`` — ``found`` False when the page
    carries none of the target processes (treat as "no data", not zero).
    Dedupes by (process, work_flow, box_type) first occurrence, mirroring
    :func:`parse_ppa_embedded_rows` (the JSON blob may repeat).
    """
    sizes = {s.lower() for s in OVERSIZE_BOX_TYPES}
    totals: dict[str, float] = {}
    seen: set[tuple[str, str, str]] = set()
    found = False
    for m in _PPA_AGG_ENTRY_RE.finditer(page_html):
        key = OVERSIZE_PROCESS_IDS.get(m.group(1))
        if not key:
            continue
        attrs = m.group(2)
        wf_m = _ATTR_WORKFLOW_RE.search(attrs)
        wf = wf_m.group(1).strip() if wf_m else ""
        if wf.lower() == "master session":
            continue
        box_m = _ATTR_BOX_TYPE_RE.search(attrs)
        box = box_m.group(1).strip() if box_m else ""
        found = True  # target process present (any size row)
        if box.lower() not in sizes:
            continue
        dedupe = (m.group(1), wf, box.lower())
        if dedupe in seen:
            continue
        seen.add(dedupe)
        units = _to_float(m.group(3))
        if units is None:
            continue
        totals[key] = totals.get(key, 0.0) + units
    if not found:
        return {"found": False, "units": None, "by_process": {}}
    return {
        "found": True,
        "units": int(round(sum(totals.values()))),
        "by_process": {k: int(round(v)) for k, v in totals.items()},
    }


def fetch_ppa_oversize_units(
    *, station: str, start: datetime, end: datetime, timeout: float = 60.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch one intraday window and return its Over units summary."""
    body = fetch_ppa_page_html(
        station=station, start=start, end=end, timeout=timeout, session=session,
    )
    return parse_ppa_oversize_units(body)


def fetch_ppa_oversize_windows(
    *, station: str, windows: list[tuple[int, int]], timeout: float = 60.0,
    max_workers: int = 3,
) -> list[dict[str, Any] | None]:
    """Fetch several intraday windows (epoch-second pairs) in parallel.

    One PPA page request per window (the rollup has no per-hour breakdown),
    sharing the Firefox cookie jar read once. Returns one summary per
    window, ``None`` for a window whose fetch failed for a non-auth reason
    (best-effort per hour). An expired session propagates as
    :class:`FclmPortalAuthError` so callers keep the 401 contract.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not windows:
        return []
    # Mint the portal session ONCE (silent Midway re-mint when the token is
    # expired) and share it across the parallel window fetches.
    sess = _fclm_live_session()
    if sess is None:
        raise FclmPortalAuthError(
            f"Sesión de FCLM no disponible. Inicia sesión en {FCLM_LOGIN_URL} en Firefox."
        )

    def one(win: tuple[int, int]) -> dict[str, Any] | None:
        start = datetime.fromtimestamp(win[0])
        end = datetime.fromtimestamp(win[1])
        return fetch_ppa_oversize_units(
            station=station, start=start, end=end, timeout=timeout, session=sess,
        )

    results: list[dict[str, Any] | None] = [None] * len(windows)
    auth_error: FclmPortalAuthError | None = None
    workers = max(1, min(max_workers, len(windows)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, win): i for i, win in enumerate(windows)}
        for fut, i in futures.items():
            try:
                results[i] = fut.result()
            except FclmPortalAuthError as exc:
                auth_error = exc
            except Exception:  # noqa: BLE001 — best-effort per window
                results[i] = None
    if auth_error is not None:
        raise auth_error
    return results


def process_columns_payload() -> list[dict[str, str]]:
    """Column list for the frontend (stable order, workbook labels)."""
    return [{"key": c["key"], "label": c["label"]} for c in PROCESS_COLUMNS]


__all__ = [
    "FCLM_LOGIN_URL",
    "QS_LOGIN_URL",
    "FclmPortalAuthError",
    "FclmPortalError",
    "FclmQsAuthError",
    "FclmQsError",
    "OVERSIZE_BOX_TYPES",
    "OVERSIZE_PROCESS_IDS",
    "PROCESS_COLUMNS",
    "actual_hours_by_column",
    "available_hours_by_column",
    "fclm_portal_login_status",
    "fclm_qs_login_status",
    "fetch_ppa_oversize_units",
    "fetch_ppa_oversize_windows",
    "fetch_ppa_page_html",
    "fetch_ppa_process_rows",
    "fetch_qs_labor_process_rows",
    "parse_ppa_embedded_rows",
    "parse_ppa_oversize_units",
    "parse_ppa_process_rows",
    "process_columns_payload",
]
