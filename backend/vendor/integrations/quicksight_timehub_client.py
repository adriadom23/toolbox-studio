"""QuickSight **TimeHub shift pattern** integration (EMBED dashboard).

Pulls the future shift roster (who is scheduled, per event date) from the
"Shift Segments" table of the TimeHub embedded dashboard, for the Forecast
UTR tool (AM):

    https://us-east-1.quicksight.aws.amazon.com/embed/<EMBED>/dashboards/
        e8b008c1-7f07-44ef-b9cf-5a80fb37f90e/sheets/…_30c52d11-…

Mapped from the operator's session HAR (2026-08-29) and the live sheet
definition:

* embed context      ``/embed/<hash>`` — MINTED BY A TO Z: the dashboard
  is an embed that ``atoz.amazon.work`` generates with a one-time
  ``?code=`` URL (``punyCodeEmbedOrigin=https://atoz.amazon.work``), and
  the hash CHANGES per embed session (2026-08-29 HAR: ``8481a6b9…``;
  2026-09-01: ``140a3f31…``). It cannot be pinned — this client
  DISCOVERS the live hash(es) from Firefox's cookie DB and tries them
  newest-first (``TIMEHUB_QS_EMBED_ID`` forces one if ever needed).
* dashboard          ``e8b008c1-7f07-44ef-b9cf-5a80fb37f90e`` (account
  556324411787, GALAXY/AD auth — NOT amzlbiaquicksight)
* sheet              ``…_30c52d11-fd11-432a-9573-e08210780371``
* table visual       ``…_a13386c8-0084-4ddf-8fde-c4a18033005f`` ("Shift
  Segments", manualMode raw table)
* prepared DS        ``prod-mv-timehub-shift-segments-tos-all-data``
  (columns are ``physicalTable.<Display Name>``)

AUTH (unlike the /sn/account clients): each embed context has its OWN
``aws-ds-access-token`` / ``aws-ds-refresh-token`` cookies scoped to
``Path=/embed/<hash>`` in Firefox — the account-scoped cookies do NOT
work here. The tokens ROTATE on every page load and the refresh token is
short-lived, so besides reading Firefox's cookie DB (path-aware; the
generic ``read_cookies_from_profile`` collapses paths) we persist the
freshest rotated pair (with its embed id) to a runtime JSON store and
try it first. RECOVERY: a dead embed session cannot be revived from the
raw QuickSight URL (it needs a fresh one-time ``code=``) — the operator
must reopen the Time Hub dashboard in A to Z, which is why
``LOGIN_URL`` points at ``atoz.amazon.work``.

DATASET PARAMETERS: the MV dataset is parameterized (location / country /
employeeId / startDate / endDate mapped from the dashboard parameters
``locationSiteIDMP`` etc.). With ``cfg.parameters`` empty the query returns
``NO_DATA_FOUND`` — the parameters below are REQUIRED. Value encoding
follows QuickSight's ``getSnapiActiveValue``: string lists as
``["A","B"]`` and times as ``parseDate('YYYY-MM-DD HH:mm:ss', …)``.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from app.integrations.firefox_cookies import (
    _copy_cookie_db,
    find_firefox_profile_dir,
)
from app.integrations.quicksight_client import (
    QuickSightAuthError,
    QuickSightError,
    websocket,
)

HOST = "us-east-1.quicksight.aws.amazon.com"
# Optional pin (normally EMPTY: the embed hash rotates per A to Z session
# and is discovered from the Firefox cookie DB).
FORCED_EMBED_ID = os.environ.get("TIMEHUB_QS_EMBED_ID", "").strip()
DASHBOARD_ID = os.environ.get("TIMEHUB_QS_DASHBOARD_ID", "").strip() or (
    "e8b008c1-7f07-44ef-b9cf-5a80fb37f90e"
)
SHEET_ID = os.environ.get("TIMEHUB_QS_SHEET_ID", "").strip() or (
    f"{DASHBOARD_ID}_30c52d11-fd11-432a-9573-e08210780371"
)
VISUAL_ID = os.environ.get("TIMEHUB_QS_VISUAL_ID", "").strip() or (
    f"{DASHBOARD_ID}_a13386c8-0084-4ddf-8fde-c4a18033005f"
)
STREAM_SOURCE_ID = os.environ.get("TIMEHUB_QS_STREAM_SOURCE_ID", "").strip() or (
    "prod-mv-timehub-shift-segments-tos-all-data"
)

# The embed is minted by A to Z with a one-time ``code=``. Always open the
# manager TimeHub landing page: A to Z owns the navigation and must not be
# redirected to an operator-specific employee-details route. Stations can pin
# another verified route through TIMEHUB_ATOZ_URL.
_LOGIN_OVERRIDE = os.environ.get("TIMEHUB_ATOZ_URL", "").strip()
_LOGIN_BASE = "https://atoz.amazon.work/timecard/managerView"


def timehub_login_url() -> str:
    return _LOGIN_OVERRIDE or _LOGIN_BASE


LOGIN_URL = timehub_login_url()

_EMBED_HASH_RE = re.compile(r"^/embed/([0-9a-f]{32})$")


def _page_url(embed_id: str) -> str:
    return f"https://{HOST}/embed/{embed_id}/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"

_P = "physicalTable"
_C_LOGIN = f"{_P}.Employee Login"
_C_NAME = f"{_P}.Employee Name"
_C_MANAGER = f"{_P}.Manager Login"
_C_MANAGER_NAME = f"{_P}.Manager Name"
_C_PATTERN = f"{_P}.Shift Pattern"
_C_STATUS = f"{_P}.Employee Status"
_C_TYPE = f"{_P}.Employee Type"
_C_EVENT = f"{_P}.Event Date"
_C_SHIFT_START = f"{_P}.Shift Start Date Time"
_C_SHIFT_END = f"{_P}.Shift End Date Time"
_C_DURATION = f"{_P}.Segment Duration in Decimals"

_GROUP = [
    _C_LOGIN, _C_NAME, _C_MANAGER, _C_MANAGER_NAME, _C_PATTERN,
    _C_STATUS, _C_TYPE, _C_EVENT, _C_SHIFT_START, _C_SHIFT_END,
]

# Dashboard parameter ids (from the embed page model) mapped onto the MV
# dataset parameters. Env-overridable in case the dashboard is revised.
_PARAM_SITE = os.environ.get("TIMEHUB_QS_PARAM_SITE", "").strip() or (
    "b4815ed4-dde7-4d67-abaa-f1b3e898e4e3"        # locationSiteIDMP
)
_PARAM_COUNTRY = os.environ.get("TIMEHUB_QS_PARAM_COUNTRY", "").strip() or (
    "a1586ec8-d57f-4f59-8ca3-e7b44b0d1b57"        # LocationCountryMP
)
_PARAM_EMPLOYEE = os.environ.get("TIMEHUB_QS_PARAM_EMPLOYEE", "").strip() or (
    "d48c4cac-00cc-459c-8fe7-acd754fac47f"        # employeeIdMP
)
_PARAM_START = os.environ.get("TIMEHUB_QS_PARAM_START", "").strip() or (
    "e28962cd-2485-4675-9d7f-639227136ac1"        # startDateMP
)
_PARAM_END = os.environ.get("TIMEHUB_QS_PARAM_END", "").strip() or (
    "825d60b5-f3cb-4289-890a-897d6a672d7c"        # endDateMP
)
_PARAM_ASOF = os.environ.get("TIMEHUB_QS_PARAM_ASOF", "").strip() or (
    "08690c4e-aa4b-4c1e-9b50-302565ccade1"        # asOfDateMP
)

# Dataset-parameter ids of the shift-segments MV (mappedDatasetParameters in
# the dashboard model). Sent ALONGSIDE the dashboard parameter ids: unknown
# keys are ignored server-side, and this covers both binding styles.
_DSPARAM = "a3996101-3d46-43dc-8f91-db54af7850ad"


class TimehubAuthError(QuickSightAuthError):
    """The embed session is gone; the operator must reopen the dashboard."""


# ---------------------------------------------------------------------------
# Cookie handling (path-scoped embed tokens + rotated-token store)
# ---------------------------------------------------------------------------

def _store_path() -> Path:
    state_dir = os.environ.get("TOOLBOX_RUNTIME_STATE_DIR", "").strip()
    base = Path(state_dir) if state_dir else Path(__file__).resolve().parents[2] / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base / "timehub_embed.json"


def _load_store() -> tuple[str, dict[str, str]]:
    """Return ``(embed_id, cookies)`` of the last working session, if any."""
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("cookies"), dict):
            embed_id = str(data.get("embed_id") or "").strip()
            if embed_id:
                return embed_id, {str(k): str(v) for k, v in data["cookies"].items()}
    except Exception:  # noqa: BLE001
        pass
    return "", {}


def _save_store(embed_id: str, cookies: dict[str, str]) -> None:
    try:
        _store_path().write_text(
            json.dumps({"saved_at": time.time(), "embed_id": embed_id,
                        "cookies": cookies}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _clear_store() -> None:
    try:
        _store_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _firefox_embed_candidates() -> list[tuple[str, dict[str, str]]]:
    """All ``(embed_id, cookies)`` sessions in Firefox, newest first.

    The embed hash is minted by A to Z per session, so the DB can hold
    several ``/embed/<hash>`` cookie paths (stale ones included) — every
    one is returned, ordered by the cookies' ``lastAccessed`` so the
    freshest session is tried first. ``read_cookies_from_profile``
    collapses cookies by name regardless of path (the account-scoped
    tokens would shadow the embed ones), so this client reads the DB
    itself.

    EXPIRED cookies are skipped (2026-09-01): Firefox never sends an
    expired cookie, but the raw DB still holds them — sending a stale
    ``aws-ds-access-token`` alongside a live refresh token gets a 401
    where the real browser recovers. An embed whose tokens are ALL
    expired is dropped entirely.
    """
    try:
        profile = find_firefox_profile_dir()
        db = _copy_cookie_db(profile)
        con = sqlite3.connect(str(db))
        cur = con.execute(
            "SELECT host, path, name, value, lastAccessed, expiry FROM moz_cookies "
            "WHERE host LIKE '%quicksight.aws.amazon.com'"
        )
        now = time.time()
        base: dict[str, str] = {}
        embeds: dict[str, dict[str, str]] = {}
        freshness: dict[str, int] = {}
        for host, path, name, value, last_accessed, expiry in cur.fetchall():
            if expiry and float(expiry) <= now:
                continue  # Firefox tampoco la enviaría
            v = (value or "").strip('"')
            if path == "/":
                base[name] = v
                continue
            m = _EMBED_HASH_RE.match(path or "")
            if not m:
                continue
            embed_id = m.group(1)
            embeds.setdefault(embed_id, {})[name] = v
            if name.startswith("aws-ds-"):
                freshness[embed_id] = max(
                    freshness.get(embed_id, 0), int(last_accessed or 0)
                )
        con.close()
        ordered = [
            e for e in sorted(embeds, key=lambda e: freshness.get(e, 0), reverse=True)
            # Sin ningún token vivo no hay sesión que probar.
            if any(n.startswith("aws-ds-") for n in embeds[e])
        ]
        if FORCED_EMBED_ID:
            ordered = [e for e in ordered if e == FORCED_EMBED_ID]
        out: list[tuple[str, dict[str, str]]] = []
        for embed_id in ordered:
            cookies = dict(base)
            cookies.update(embeds[embed_id])
            out.append((embed_id, cookies))
        return out
    except Exception:  # noqa: BLE001
        return []


def _try_page(
    embed_id: str, cookies: dict[str, str], timeout: float,
) -> tuple[str, dict[str, str], str]:
    """GET the embed page and classify the result.

    Returns ``(mbtc, merged_cookies, outcome)`` where outcome is ``ok``,
    ``rejected`` (explicit 401/403) or ``transient``. A to Z can briefly
    expose a 200 bootstrap page without the csrf token while minting a new
    embed; retry those responses in the same session so rotated cookies are
    not replayed by the reconnect loop.
    """
    sess = requests.Session()
    for name, value in cookies.items():
        sess.cookies.set(name, value, domain=".quicksight.aws.amazon.com", path="/")

    merged: dict[str, str] = {}
    outcome = "transient"
    for attempt in range(3):
        try:
            resp = sess.get(
                _page_url(embed_id),
                headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
                timeout=timeout, allow_redirects=True,
            )
        except requests.RequestException:
            outcome = "transient"
        else:
            merged = {c.name: c.value for c in sess.cookies}
            if resp.status_code in (401, 403):
                return "", merged, "rejected"
            if resp.status_code == 200:
                match = re.search(r'"csrf"\s*:\s*"([^"]+)"', _html.unescape(resp.text))
                if match:
                    return match.group(1), merged, "ok"
                outcome = "transient"
            elif resp.status_code == 429 or resp.status_code >= 500:
                outcome = "transient"
            else:
                return "", merged, "rejected"
        if attempt < 2:
            time.sleep(1.0)
    return "", merged, outcome


# In-memory validated session + single-flight lock (2026-09-01). Every
# successful page GET ROTATES the token pair server-side; concurrent
# probes (60s poll + 4s reconnect loop + data fetches, or a second
# process) each doing their own GET trip QuickSight's refresh-token
# REUSE DETECTION and kill the whole token family — the observed
# "worked, then died mid-afternoon". One process therefore rotates at
# most once per TTL window and every caller shares the validated triple.
_SESSION_TTL_S = 8 * 60.0
_session_lock = threading.Lock()
# Todos los consumidores del embed (Forecast UTR y Reportes HR) comparten
# una familia de refresh tokens. Las consultas se serializan para que dos
# vistas abiertas en paralelo no disparen la detección de reutilización.
_query_lock = threading.Lock()
_session: dict[str, Any] = {"embed_id": "", "cookies": {}, "mbtc": "", "at": 0.0}
# Explicit auth rejection gets a long quarantine to avoid refresh-token reuse.
# Transport errors and A to Z's temporary bootstrap page only get one poll
# interval: they are normal while a fresh embed is still being minted.
_CANDIDATE_BACKOFF_S = 90.0
_TRANSIENT_BACKOFF_S = 4.0
_failed_candidates: dict[str, float] = {}


def _candidate_fingerprint(embed_id: str, cookies: dict[str, str]) -> str:
    token_pairs = sorted(
        (name, value)
        for name, value in cookies.items()
        if name.startswith("aws-ds-")
    )
    raw = json.dumps([embed_id, token_pairs], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invalidate_session() -> None:
    """Drop the cached session; the next open re-validates from scratch."""
    with _session_lock:
        _session.update(embed_id="", cookies={}, mbtc="", at=0.0)


def _open_session(
    timeout: float = 30.0, *, fresh: bool = False,
) -> tuple[str, dict[str, str], str]:
    """Resolve a live embed session: ``(embed_id, cookies, mbtc)``.

    Serves the in-memory session while it is younger than the TTL (no
    network, no token rotation). On miss, tries the rotated store first,
    then every ``/embed/<hash>`` session found in Firefox (newest first —
    A to Z mints a new hash per session, so stale ones can coexist with
    the live one). The lock single-flights concurrent callers.
    """
    with _session_lock:
        age = time.time() - float(_session["at"] or 0)
        if not fresh and _session["mbtc"] and age < _SESSION_TTL_S:
            return _session["embed_id"], dict(_session["cookies"]), _session["mbtc"]

        candidates: list[tuple[str, str, dict[str, str], str]] = []
        seen_fingerprints: set[str] = set()
        now = time.time()
        # Drop expired quarantine entries opportunistically.
        for fingerprint, until in list(_failed_candidates.items()):
            if until <= now:
                _failed_candidates.pop(fingerprint, None)

        def add_candidate(source: str, embed_id: str, cookies: dict[str, str]) -> None:
            fingerprint = _candidate_fingerprint(embed_id, cookies)
            if fingerprint in seen_fingerprints:
                return
            seen_fingerprints.add(fingerprint)
            if _failed_candidates.get(fingerprint, 0.0) > now:
                return
            candidates.append((source, embed_id, cookies, fingerprint))

        store_id, store_cookies = _load_store()
        if store_id and store_cookies and (
            not FORCED_EMBED_ID or store_id == FORCED_EMBED_ID
        ):
            add_candidate("store", store_id, store_cookies)
        firefox = _firefox_embed_candidates()
        for embed_id, cookies in firefox:
            add_candidate("db", embed_id, cookies)

        for source, embed_id, cookies, fingerprint in candidates:
            mbtc, merged, outcome = _try_page(embed_id, cookies, timeout)
            if mbtc:
                _failed_candidates.pop(fingerprint, None)
                _save_store(embed_id, merged)
                _session.update(
                    embed_id=embed_id, cookies=dict(merged), mbtc=mbtc, at=time.time(),
                )
                return embed_id, merged, mbtc
            backoff = (
                _CANDIDATE_BACKOFF_S if outcome == "rejected"
                else _TRANSIENT_BACKOFF_S
            )
            _failed_candidates[fingerprint] = time.time() + backoff
            if source == "store" and outcome == "rejected":
                _clear_store()
        _session.update(embed_id="", cookies={}, mbtc="", at=0.0)
        if not firefox:
            raise TimehubAuthError(
                "No hay sesion del dashboard de shift pattern en Firefox. "
                "Abre el Time Hub desde A to Z."
            )
        raise TimehubAuthError(
            "La sesion del dashboard de shift pattern expiro. "
            "Abre el Time Hub desde A to Z en Firefox."
        )


def timehub_login_status(timeout: float = 20.0) -> dict[str, Any]:
    """Whether the Pay Summary embed session is available.

    A running report already owns ``_query_lock`` and necessarily resolved a
    session before opening its visual streams. Do not queue a connection probe
    behind that long extraction: report the cached session as busy/ready so the
    connections panel stays stable and, critically, no second page GET rotates
    the shared refresh token mid-query.
    """
    acquired = _query_lock.acquire(blocking=False)
    if not acquired:
        with _session_lock:
            age = time.time() - float(_session["at"] or 0)
            ready = bool(_session["mbtc"] and age < _SESSION_TTL_S)
        return {
            "ready": ready,
            "busy": True,
            "login_url": timehub_login_url(),
            "host": HOST,
        }
    try:
        _open_session(timeout)
        return {"ready": True, "login_url": timehub_login_url(), "host": HOST}
    except TimehubAuthError as exc:
        return {"ready": False, "login_url": timehub_login_url(), "host": HOST,
                "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ready": False, "login_url": timehub_login_url(), "host": HOST,
                "reason": f"error: {exc}"}
    finally:
        _query_lock.release()


# ---------------------------------------------------------------------------
# WebSocket streaming (embed path)
# ---------------------------------------------------------------------------

def _connect_ws(embed_id: str, cookies: dict[str, str], mbtc: str, timeout: float):
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")
    ws_url = (
        f"wss://{HOST}/embed/{embed_id}/websocket/?mbtc={mbtc}"
        f"&pageType=EmbeddingDashboard"
    )
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        return websocket.create_connection(
            ws_url,
            header=[f"User-Agent: {_UA}", f"Origin: https://{HOST}"],
            cookie=cookie_header, timeout=timeout, suppress_origin=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise TimehubAuthError(
            f"No se pudo conectar al WebSocket del shift pattern: {str(exc)[:200]}"
        )


def _stream_vis(start_vis: dict[str, Any], embed_id: str,
                cookies: dict[str, str], mbtc: str,
                timeout: float) -> dict[str, Any] | None:
    """Send START_VIS and wait for the data frame (or NO_DATA_FOUND -> None)."""
    ws = _connect_ws(embed_id, cookies, mbtc, timeout)
    cid = start_vis["cid"]
    data_msg: dict[str, Any] | None = None
    no_data = False
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
            if msg.get("status") == "NO_DATA_FOUND":
                no_data = True
                break
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
    if no_data:
        return {"data": [], "available": 0}
    return data_msg


def _time_param(date_iso: str) -> str:
    return f"parseDate('{date_iso} 00:00:00', 'yyyy-MM-dd HH:mm:ss')"


def _build_parameters(site: str, date_from: str, date_to: str) -> dict[str, str]:
    site_list = json.dumps([site])
    params = {
        # Dashboard parameter ids (getActiveValueForSnapi keys them by id).
        _PARAM_SITE: site_list,
        _PARAM_COUNTRY: "[]",
        _PARAM_EMPLOYEE: "[]",
        _PARAM_START: _time_param(date_from),
        _PARAM_END: _time_param(date_to),
        _PARAM_ASOF: "truncDate('DD', now())",
        # Dataset-scoped ids (mappedDatasetParameters); unknown keys are
        # ignored, so sending both binding styles is harmless.
        f"{_DSPARAM}.locationParameter": site_list,
        f"{_DSPARAM}.locationCountryParameter": "[]",
        f"{_DSPARAM}.employeeIdParameter": "[]",
        f"{_DSPARAM}.startDateParameter": _time_param(date_from),
        f"{_DSPARAM}.endDateParameter": _time_param(date_to),
    }
    return params


def _build_start_vis(site: str, date_from: str, date_to: str) -> dict[str, Any]:
    last = len(_GROUP) - 1
    fields = []
    for i, cid in enumerate(_GROUP):
        if i == last:
            fields.append({"name": cid, "limit": 50000,
                           "sort": {"name": _C_EVENT, "dir": "ASC"},
                           "chartContext": cid, "offset": 0, "partitioned": False})
        else:
            fields.append({"name": cid, "limit": -1, "sort": None,
                           "chartContext": cid, "offset": -1, "partitioned": False})
    metrics = [{"name": _C_DURATION, "func": "SUM", "chartContext": _C_DURATION}]
    return {
        "type": "START_VIS", "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {"excludeCountStar": True, "group": {"fields": fields, "metrics": metrics},
                    "clsSupport": True, "calculatedFields": [],
                    "filterExpressions": [],
                    "parameters": _build_parameters(site, date_from, date_to)},
            "columnNamingMode": "ID", "speed": 1, "tz": "Europe/Madrid",
            "streamSourceId": STREAM_SOURCE_ID, "datasetVersionAlias": "$STABLE",
            "enableComputations": False, "computationsOnly": False,
            "sheetId": SHEET_ID, "visualId": VISUAL_ID, "visualArn": None,
            "visualType": "TABLE", "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()), "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _clean_dt(value: Any) -> str:
    """Normalize QuickSight datetime group values ('2026-09-06 01:00:00.000')."""
    s = str(value or "").strip()
    return s[:16] if len(s) >= 16 else s


def _fetch_shift_roster_unlocked(
    *, site: str, date_from: str, date_to: str, timeout: float = 90.0,
) -> dict[str, Any]:
    """Scheduled shifts per employee/day for ``site`` in [date_from, date_to].

    Returns ``{site, date_from, date_to, rows}``; each row is one employee x
    event date: ``{login, name, manager, manager_name, pattern, status,
    type, date, shift_start, shift_end}``.
    """
    site = (site or "").strip().upper()
    if not site:
        raise QuickSightError("Falta la estacion para el fetch del shift pattern.")
    embed_id, cookies, mbtc = _open_session(timeout=min(timeout, 30.0))

    start_vis = _build_start_vis(site, date_from, date_to)

    def stream_current_session() -> dict[str, Any]:
        result = _stream_vis(start_vis, embed_id, cookies, mbtc, timeout)
        if result is None:
            raise QuickSightError("QuickSight no devolvio datos del shift pattern.")
        return result

    try:
        data = stream_current_session()
    except (TimehubAuthError, QuickSightError):
        # La sesión cacheada pudo quedar rotada por otro consumidor (pestaña
        # del navegador, otro proceso), o el WebSocket pudo terminar sin
        # entregar frame: re-validar UNA vez y reintentar ambos casos.
        invalidate_session()
        embed_id, cookies, mbtc = _open_session(timeout=min(timeout, 30.0), fresh=True)
        data = stream_current_session()

    rows: list[dict[str, Any]] = []
    for item in data.get("data", []):
        g = item.get("group", [])
        if len(g) < len(_GROUP):
            continue
        date_iso = str(g[7])[:10]
        if not date_iso:
            continue
        rows.append({
            "login": str(g[0] or "").strip(),
            "name": str(g[1] or "").strip(),
            "manager": str(g[2] or "").strip(),
            "manager_name": str(g[3] or "").strip(),
            "pattern": str(g[4] or "").strip(),
            "status": str(g[5] or "").strip(),
            "type": str(g[6] or "").strip(),
            "date": date_iso,
            "shift_start": _clean_dt(g[8]),
            "shift_end": _clean_dt(g[9]),
        })

    rows.sort(key=lambda r: (r["date"], r["login"]))
    return {"site": site, "date_from": date_from, "date_to": date_to, "rows": rows}


def fetch_shift_roster(
    *, site: str, date_from: str, date_to: str, timeout: float = 90.0,
) -> dict[str, Any]:
    """Serialized public shift-roster fetch for the shared TimeHub embed."""
    with _query_lock:
        return _fetch_shift_roster_unlocked(
            site=site, date_from=date_from, date_to=date_to, timeout=timeout
        )


# ---------------------------------------------------------------------------
# Generic TimeHub report extraction (Balance Summary / Pay Summary)
# ---------------------------------------------------------------------------

_REPORT_META_TTL_S = 15 * 60.0
_report_meta_lock = threading.Lock()
_report_meta_cache: dict[str, Any] = {"embed_id": "", "at": 0.0, "sheets": []}


def _display_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        preferred = ("text", "value", "title", "name", "label", "unformatted")
        for key in preferred:
            text = _display_text(value.get(key))
            if text:
                return text
        for key, nested in value.items():
            if key in preferred:
                continue
            text = _display_text(nested)
            if text:
                return text
    if isinstance(value, list):
        for item in value:
            text = _display_text(item)
            if text:
                return text
    return ""


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _embed_api_get(
    embed_id: str,
    cookies: dict[str, str],
    mbtc: str,
    *,
    sheet_id: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    suffix = f"/sheets/{sheet_id}" if sheet_id else ""
    url = f"https://{HOST}/embed/{embed_id}/api/dashboards/{DASHBOARD_ID}{suffix}"
    params: dict[str, Any] = {
        "Operation": "GetSheetForDashboard" if sheet_id else "GetDashboard",
        "mbtc": mbtc,
    }
    if sheet_id:
        params.update(
            pageSize=250,
            customViewSubentityOptions="IGNORE_CUSTOM_VIEW",
        )
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": _page_url(embed_id),
    }
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(
            name, value, domain=".quicksight.aws.amazon.com", path="/"
        )
    try:
        response = session.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise QuickSightError(f"TimeHub metadata request failed: {exc}") from exc
    if response.status_code in (301, 302, 401, 403):
        raise TimehubAuthError("La sesion del dashboard de TimeHub expiro.")
    if response.status_code != 200:
        raise QuickSightError(
            f"TimeHub metadata HTTP {response.status_code}: {response.text[:300]}"
        )

    # Los GET de metadata también pueden rotar los tokens del embed. Capturar
    # el Set-Cookie antes de la siguiente hoja evita reutilizar el refresh
    # anterior y mantiene sincronizados el store y todos los consumidores.
    merged = {cookie.name: cookie.value for cookie in session.cookies}
    if merged:
        cookies.clear()
        cookies.update(merged)
        _save_store(embed_id, merged)
        with _session_lock:
            if _session["embed_id"] == embed_id:
                _session.update(cookies=dict(merged), at=time.time())
    try:
        payload = response.json()
    except ValueError as exc:
        raise QuickSightError("TimeHub devolvio metadata no valida.") from exc
    if not isinstance(payload, dict):
        raise QuickSightError("TimeHub devolvio una metadata inesperada.")
    return payload


def _sheet_entries(metadata: dict[str, Any]) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            sheet_id = str(node.get("sheetId") or "").strip()
            if sheet_id:
                name = _display_text(
                    node.get("sheetName") or node.get("name") or node.get("title")
                )
                current = found.get(sheet_id)
                if current is None or (not current["name"] and name):
                    found[sheet_id] = {"sheet_id": sheet_id, "name": name}
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(metadata)
    return list(found.values())


def _visual_entries(sheet_id: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            visual_id = str(node.get("visualId") or "").strip()
            axes = node.get("appliedAxes")
            if visual_id and isinstance(axes, list):
                columns: list[dict[str, str]] = []
                for axis in axes:
                    if not isinstance(axis, dict):
                        continue
                    column_id = str(axis.get("columnId") or "").strip()
                    if not column_id:
                        continue
                    columns.append(
                        {
                            "id": column_id,
                            "name": (
                                _display_text(axis.get("customLabels"))
                                or _display_text(axis.get("customLabel"))
                                or str(axis.get("columnName") or column_id).strip()
                            ),
                            "axis": str(axis.get("axisName") or "").strip().upper(),
                            "agg": str(axis.get("aggregationFunction") or "").strip().upper(),
                        }
                    )
                candidate = {
                    "sheet_id": sheet_id,
                    "visual_id": visual_id,
                    "title": _display_text(
                        node.get("visualTitle") or node.get("title") or node.get("name")
                    ),
                    "type": str(node.get("visualType") or node.get("type") or "TABLE").upper(),
                    "stream_source_id": str(node.get("preparedDataSourceId") or "").strip(),
                    "columns": columns,
                }
                previous = found.get(visual_id)
                score = len(columns) + (5 if candidate["stream_source_id"] else 0) + (
                    2 if candidate["title"] else 0
                )
                old_score = -1
                if previous:
                    old_score = len(previous["columns"]) + (
                        5 if previous["stream_source_id"] else 0
                    ) + (2 if previous["title"] else 0)
                if score > old_score:
                    found[visual_id] = candidate
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(metadata)
    return list(found.values())


def _report_catalog(
    embed_id: str,
    cookies: dict[str, str],
    mbtc: str,
    timeout: float,
) -> list[dict[str, Any]]:
    with _report_meta_lock:
        age = time.time() - float(_report_meta_cache["at"] or 0)
        if (
            _report_meta_cache["embed_id"] == embed_id
            and _report_meta_cache["sheets"]
            and age < _REPORT_META_TTL_S
        ):
            return list(_report_meta_cache["sheets"])

        dashboard = _embed_api_get(
            embed_id, cookies, mbtc, timeout=min(timeout, 30.0)
        )
        sheets: list[dict[str, Any]] = []
        for sheet in _sheet_entries(dashboard):
            definition = _embed_api_get(
                embed_id,
                cookies,
                mbtc,
                sheet_id=sheet["sheet_id"],
                timeout=min(timeout, 30.0),
            )
            sheets.append(
                {
                    **sheet,
                    "visuals": _visual_entries(sheet["sheet_id"], definition),
                }
            )
        _report_meta_cache.update(embed_id=embed_id, at=time.time(), sheets=sheets)
        return list(sheets)


def _axis_kind(axis: str) -> str:
    upper = str(axis or "").upper()
    if any(token in upper for token in ("GROUP", "ROW", "COLUMN")):
        return "group"
    if any(token in upper for token in ("VALUE", "METRIC")):
        return "metric"
    return "group"


def _visual_start_vis(
    visual: dict[str, Any],
    *,
    site: str,
    date_from: str,
    date_to: str,
    offset: int = 0,
) -> dict[str, Any]:
    groups = [c for c in visual["columns"] if _axis_kind(c["axis"]) == "group"]
    metrics = [c for c in visual["columns"] if _axis_kind(c["axis"]) == "metric"]
    fields: list[dict[str, Any]] = []
    for index, column in enumerate(groups):
        is_last = index == len(groups) - 1
        fields.append(
            {
                "name": column["id"],
                "limit": 50000 if is_last else -1,
                "sort": (
                    {"name": column["id"], "dir": "ASC"} if is_last else None
                ),
                "chartContext": column["id"],
                "offset": offset if is_last else -1,
                "partitioned": False,
            }
        )
    metric_fields = [
        {
            "name": column["id"],
            "func": column["agg"] or "SUM",
            "chartContext": column["id"],
        }
        for column in metrics
    ]
    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metric_fields},
                "clsSupport": True,
                "calculatedFields": [],
                "filterExpressions": [],
                "parameters": _build_parameters(site, date_from, date_to),
            },
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": "Europe/Madrid",
            "streamSourceId": visual["stream_source_id"],
            "datasetVersionAlias": "$STABLE",
            "enableComputations": False,
            "computationsOnly": False,
            "sheetId": visual["sheet_id"],
            "visualId": visual["visual_id"],
            "visualArn": None,
            "visualType": visual["type"] or "TABLE",
            "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()),
            "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _metric_cell_value(cell: Any) -> Any:
    if not isinstance(cell, dict):
        return cell
    for key in ("value", "metricValue", "rawValue", "formattedValue"):
        if key in cell:
            return cell[key]
    if len(cell) == 1:
        return next(iter(cell.values()))
    return None


def _report_rows(
    visual: dict[str, Any], data: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    groups = [c for c in visual["columns"] if _axis_kind(c["axis"]) == "group"]
    metrics = [c for c in visual["columns"] if _axis_kind(c["axis"]) == "metric"]
    columns = [
        {"key": c["id"], "label": c["name"], "kind": "group"} for c in groups
    ] + [
        {"key": c["id"], "label": c["name"], "kind": "metric"} for c in metrics
    ]
    rows: list[dict[str, Any]] = []
    for item in data.get("data", []):
        group = item.get("group", [])
        metric_cells = (item.get("current") or {}).get("metrics", {})
        row: dict[str, Any] = {}
        for index, column in enumerate(groups):
            row[column["id"]] = group[index] if index < len(group) else None
        for column in metrics:
            row[column["id"]] = _metric_cell_value(metric_cells.get(column["id"]))
        rows.append(row)
    return rows, columns


def _target_matches(value: str, targets: list[str]) -> bool:
    normalized = _norm(value)
    return any(_norm(target) in normalized for target in targets if _norm(target))


def _fetch_timehub_report_once(
    *,
    report_names: list[str],
    site: str,
    date_from: str,
    date_to: str,
    timeout: float,
    fresh: bool = False,
) -> dict[str, Any]:
    embed_id, cookies, mbtc = _open_session(
        timeout=min(timeout, 30.0), fresh=fresh
    )
    catalog = _report_catalog(embed_id, cookies, mbtc, timeout)
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for sheet in catalog:
        matching_visuals = [
            visual
            for visual in sheet["visuals"]
            if _target_matches(visual["title"], report_names)
        ]
        if matching_visuals:
            selected.extend((sheet, visual) for visual in matching_visuals)
        elif _target_matches(sheet["name"], report_names):
            selected.extend((sheet, visual) for visual in sheet["visuals"])
    if not selected:
        available = sorted(
            {
                name
                for sheet in catalog
                for name in [sheet["name"], *[v["title"] for v in sheet["visuals"]]]
                if name
            }
        )
        raise QuickSightError(
            f"TimeHub report not found ({', '.join(report_names)}). "
            f"Available: {', '.join(available[:80])}"
        )

    sections: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for sheet, visual in selected:
        if not visual["stream_source_id"] or not visual["columns"]:
            continue
        try:
            all_items: list[dict[str, Any]] = []
            available_count = 0
            offset = 0
            seen_pages: set[str] = set()
            complete = True
            for _page in range(50):
                start_vis = _visual_start_vis(
                    visual,
                    site=site,
                    date_from=date_from,
                    date_to=date_to,
                    offset=offset,
                )
                data = _stream_vis(start_vis, embed_id, cookies, mbtc, timeout)
                if data is None:
                    raise QuickSightError("QuickSight no devolvio un frame de datos.")
                page_items = list(data.get("data", []))
                available_count = max(
                    available_count, int(data.get("available") or len(page_items))
                )
                if not page_items:
                    break
                fingerprint = json.dumps(
                    page_items, sort_keys=True, default=str, separators=(",", ":")
                )
                if fingerprint in seen_pages:
                    complete = False
                    break
                seen_pages.add(fingerprint)
                all_items.extend(page_items)
                if len(all_items) >= available_count:
                    break
                offset += len(page_items)
            if len(all_items) < available_count:
                complete = False
                errors.append(
                    {
                        "title": visual["title"] or sheet["name"],
                        "visual_id": visual["visual_id"],
                        "message": (
                            f"QuickSight returned {len(all_items)} of "
                            f"{available_count} available rows."
                        ),
                    }
                )
            rows, columns = _report_rows(visual, {"data": all_items})
            sections.append(
                {
                    "sheet": sheet["name"],
                    "title": visual["title"] or sheet["name"],
                    "visual_id": visual["visual_id"],
                    "columns": columns,
                    "rows": rows,
                    "available": available_count or len(rows),
                    "complete": complete,
                }
            )
        except QuickSightError as exc:
            errors.append(
                {
                    "title": visual["title"] or sheet["name"],
                    "visual_id": visual["visual_id"],
                    "message": str(exc),
                }
            )
    if not sections and errors:
        raise QuickSightError(errors[0]["message"])
    return {
        "site": site,
        "date_from": date_from,
        "date_to": date_to,
        "report": report_names[0],
        "sections": sections,
        "errors": errors,
    }


def _fetch_timehub_report_unlocked(
    *,
    report_names: list[str],
    site: str,
    date_from: str,
    date_to: str,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Return every queryable visual from a named TimeHub report."""
    site = (site or "").strip().upper()
    if not site:
        raise QuickSightError("Falta la estacion para el reporte de TimeHub.")
    try:
        return _fetch_timehub_report_once(
            report_names=report_names,
            site=site,
            date_from=date_from,
            date_to=date_to,
            timeout=timeout,
        )
    except TimehubAuthError:
        invalidate_session()
        return _fetch_timehub_report_once(
            report_names=report_names,
            site=site,
            date_from=date_from,
            date_to=date_to,
            timeout=timeout,
            fresh=True,
        )


def fetch_timehub_report(
    *,
    report_names: list[str],
    site: str,
    date_from: str,
    date_to: str,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Serialized public report fetch for the shared TimeHub embed."""
    with _query_lock:
        return _fetch_timehub_report_unlocked(
            report_names=report_names,
            site=site,
            date_from=date_from,
            date_to=date_to,
            timeout=timeout,
        )


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip().replace("%", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def fetch_flex_balances(
    *, site: str, date_from: str, date_to: str, timeout: float = 90.0,
) -> dict[str, Any]:
    """Normalize positive/negative flex totals per associate."""
    report = fetch_timehub_report(
        report_names=["Balance Summary Report", "Balance Summary"],
        site=site,
        date_from=date_from,
        date_to=date_to,
        timeout=timeout,
    )
    totals: dict[str, dict[str, Any]] = {}
    matched_columns: set[str] = set()

    def add_balance(login: str, field: str, value: float, label: str) -> None:
        cell = totals.setdefault(
            login,
            {"login": login, "positive_flex": None, "negative_flex": None},
        )
        current = cell[field]
        cell[field] = value if current is None else current + value
        matched_columns.add(label)

    for section in report["sections"]:
        labels = {column["key"]: _norm(column["label"]) for column in section["columns"]}
        login_key = next(
            (
                key
                for key, label in labels.items()
                if "employee login" in label
                or label in ("login", "employee id", "alias", "username")
            ),
            None,
        )
        positive_key = next(
            (
                key
                for key, label in labels.items()
                if ("positive" in label and ("flex" in label or "balance" in label))
                or "positive flex" in label
            ),
            None,
        )
        negative_key = next(
            (
                key
                for key, label in labels.items()
                if ("negative" in label and ("flex" in label or "balance" in label))
                or "negative flex" in label
            ),
            None,
        )
        category_key = next(
            (
                key
                for key, label in labels.items()
                if "flex" in label and any(token in label for token in ("type", "category", "direction"))
            ),
            None,
        )
        value_key = next(
            (
                key
                for key, label in labels.items()
                if key not in (login_key, category_key)
                and any(token in label for token in ("balance", "hours", "duration", "amount"))
            ),
            None,
        )
        if not login_key:
            continue
        for row in section["rows"]:
            login = str(row.get(login_key) or "").strip().lower()
            if not login:
                continue
            if positive_key or negative_key:
                if positive_key:
                    value = _numeric(row.get(positive_key))
                    if value is not None:
                        add_balance(login, "positive_flex", value, labels[positive_key])
                if negative_key:
                    value = _numeric(row.get(negative_key))
                    if value is not None:
                        add_balance(login, "negative_flex", value, labels[negative_key])
            elif category_key and value_key:
                category = _norm(row.get(category_key))
                value = _numeric(row.get(value_key))
                if value is None:
                    continue
                if any(token in category for token in ("positive", "flex up", "flexi positive")):
                    add_balance(login, "positive_flex", value, labels[value_key])
                elif any(token in category for token in ("negative", "flex down", "flexi negative")):
                    add_balance(login, "negative_flex", value, labels[value_key])
    rows = sorted(totals.values(), key=lambda row: row["login"])
    return {
        "site": site,
        "date_from": date_from,
        "date_to": date_to,
        "rows": rows,
        "matched_columns": sorted(matched_columns),
        "sections": report["sections"],
        "errors": report["errors"],
    }


def fetch_pay_summary(
    *, site: str, date_from: str, date_to: str, timeout: float = 90.0,
) -> dict[str, Any]:
    """Return all queryable information shown in Reports -> Pay Summary."""
    return fetch_timehub_report(
        report_names=["Pay Summary"],
        site=site,
        date_from=date_from,
        date_to=date_to,
        timeout=timeout,
    )


__all__ = [
    "DASHBOARD_ID",
    "FORCED_EMBED_ID",
    "LOGIN_URL",
    "QuickSightError",
    "TimehubAuthError",
    "fetch_flex_balances",
    "fetch_pay_summary",
    "fetch_shift_roster",
    "fetch_timehub_report",
    "invalidate_session",
    "timehub_login_status",
    "timehub_login_url",
]
