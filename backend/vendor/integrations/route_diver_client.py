"""Route Diver / RouteIQ client — resolve a route id to its driver (DA).

WUBL (``wubl60drivercount``) rows identify early-finishing routes by
``assigned_route_id`` but leave ``transporter_id`` blank (``"-"``). To
turn a route into a DA we hit RouteIQ's ``cachedRouteDiveResult``
endpoint, exactly like CORE Pilot's ``enrich_wubl_drivers`` did, and
read the first ``driverStoryList`` entry's ``employeeId``.

Auth: the endpoint sits behind Midway, so we read the ``amzn_sso_token``
JWT from the user's Firefox profile (same mechanism as PerfectMile /
Mercury / Metis) and ride the warm-up redirect that mints the session
cookies. No browser window is opened.

Public API:

* :func:`has_session` — quick check that we hold a usable SSO token.
* :func:`enrich_routes` — map a batch of route ids to driver ids,
  resolved in parallel. Unknown routes are simply omitted.
"""
from __future__ import annotations

import re
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor

import requests

from app.integrations.firefox_cookies import (
    ROUTEDIVER_REQUIRED_COOKIES,
    has_required,
    routediver_cookies_from_profile,
)

# Silence the InsecureRequestWarning we accept by passing ``verify=False``
# (mirrors the rest of the internal-Amazon clients in this codebase).
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

ROUTE_DIVER_HOME = "https://eu.eagle.routeiq.last-mile.amazon.dev/"
ROUTE_DIVER_URL = "https://eu.eagle.routeiq.last-mile.amazon.dev/api/v1/cachedRouteDiveResult"
ROUTE_DIVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept": "*/*",
    "Accept-Language": "en-GB,en;q=0.5",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://eu.eagle.routeiq.last-mile.amazon.dev",
    "Referer": "https://eu.eagle.routeiq.last-mile.amazon.dev/",
}

# Route Diver tolerates a healthy amount of concurrency; CORE used 10.
ENRICH_WORKERS = 10
REQUEST_TIMEOUT = 30


def read_cookies() -> dict[str, str]:
    """Return the Route Diver SSO cookies from the Firefox profile."""
    try:
        return routediver_cookies_from_profile() or {}
    except Exception:  # noqa: BLE001
        return {}


def _build_session(cookies: dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(ROUTE_DIVER_HEADERS)
    session.verify = False
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=ENRICH_WORKERS,
        pool_maxsize=ENRICH_WORKERS,
    )
    session.mount("https://", adapter)
    return session


def _warmup(session: requests.Session) -> None:
    """GET the SPA root so Midway re-issues the session cookies."""
    try:
        resp = session.get(
            ROUTE_DIVER_HOME,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        for name, value in resp.cookies.items():
            session.cookies.set(name, value)
    except requests.RequestException:
        pass


def has_session() -> bool:
    """True when we hold the SSO token Route Diver needs."""
    return has_required(read_cookies(), ROUTEDIVER_REQUIRED_COOKIES)


# Amazon transporter ids look like ``A1PFXOL0R6VKJX`` — an ``A`` followed
# by ~12+ uppercase alphanumerics. This is the SAME identifier PerfectMile
# exposes as ``lt_attempt_transporter_id`` (the DA shown in Concessions x
# DA), so it is what we must return to make the join work. Route Diver's
# ``employeeId`` is a different namespace and would never match.
_TRANSPORTER_RE = re.compile(r"^A[0-9A-Z]{12,}$")

# Field names (lower-cased, dots/underscores stripped) that hold the
# transporter id across Route Diver response shapes, in priority order.
_TRANSPORTER_KEYS = ("transporterid", "drivertransporterid", "drivercustomerid", "customerid")


def _deep_find_transporter(node) -> str:
    """Walk a JSON-ish structure and return the first transporter-format id."""
    if isinstance(node, dict):
        # Prefer explicitly-named transporter fields first.
        for key, value in node.items():
            norm = str(key).lower().replace("_", "").replace(".", "")
            if norm in _TRANSPORTER_KEYS:
                v = str(value or "").strip()
                if v and v != "-":
                    return v
        # Then any value that looks like a transporter id.
        for value in node.values():
            if isinstance(value, str) and _TRANSPORTER_RE.match(value.strip()):
                return value.strip()
            found = _deep_find_transporter(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_find_transporter(item)
            if found:
                return found
    return ""


def _driver_from_payload(data: dict) -> str:
    """Pull the DA's transporter id out of a cachedRouteDiveResult body.

    We want the transporter id (``A1...``) so it matches the concession
    DA. We search the route-dive result for a transporter-format id and
    only fall back to ``employeeId`` if nothing else is present.
    """
    result = (data or {}).get("routeDiveResult", {})
    transporter = _deep_find_transporter(result) or _deep_find_transporter(data or {})
    if transporter:
        return transporter
    try:
        driver_list = result.get("actuals", {}).get("driverStoryList", [])
    except AttributeError:
        return ""
    if driver_list:
        emp = driver_list[0].get("employeeId")
        return str(emp).strip() if emp else ""
    return ""


def enrich_routes(route_ids):
    """Resolve route ids to driver (employee) ids in parallel.

    Returns ``(mapping, stats)`` where ``mapping`` is
    ``{route_id: driver_id}`` (only routes that resolved) and ``stats``
    is ``{"session": bool, "looked_up": int, "found": int}``.
    """
    unique = [r for r in dict.fromkeys(str(r).strip() for r in route_ids) if r]
    if not unique:
        return {}, {"session": has_session(), "looked_up": 0, "found": 0}

    cookies = read_cookies()
    if not has_required(cookies, ROUTEDIVER_REQUIRED_COOKIES):
        return {}, {"session": False, "looked_up": 0, "found": 0}

    session = _build_session(cookies)
    _warmup(session)

    first_log = threading.Event()

    def _lookup(route_id):
        try:
            resp = session.post(
                ROUTE_DIVER_URL,
                json={"routeId": route_id},
                timeout=REQUEST_TIMEOUT,
            )
            if not first_log.is_set():
                first_log.set()
                print(
                    f"[RouteDiver] first call route={route_id} status={resp.status_code}",
                    flush=True,
                )
            if resp.status_code != 200:
                return route_id, ""
            return route_id, _driver_from_payload(resp.json())
        except Exception:  # noqa: BLE001
            return route_id, ""

    mapping: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(ENRICH_WORKERS, len(unique))) as executor:
        for route_id, driver in executor.map(_lookup, unique):
            if driver:
                mapping[route_id] = driver

    return mapping, {
        "session": True,
        "looked_up": len(unique),
        "found": len(mapping),
    }
