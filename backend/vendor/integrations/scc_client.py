"""SCC ("boson") station dashboard client — ``logistics.amazon.co.uk``.

Talks to the station proxy API gateway the same way the SCC web app does,
so tools can pull package data server-side instead of asking the operator
to export a CSV from the dashboard and upload it.

    POST https://logistics.amazon.co.uk/station/proxyapigateway/data

The gateway is a thin proxy: the JSON body selects the downstream backend
(``processName``) and operation (``resourcePath``); everything else rides
in ``requestBody`` / ``requestParams``. Examples captured from the web app:

* ``oculus`` / ``/os/batchGetPackageSummary`` — current state of a list of
  tracking IDs (``currentPackageState``: RECEIVED / DELIVERY_FAILED / ...).
* ``oculus`` / ``/os/getDwellingPackageData`` — ageing/dwell summary counts.
* ``oculus`` / ``/os/getDrillDownData`` — package-level rows for a view.

Auth mirrors the browser and reuses the same Firefox-cookie machinery as
the Cortex client:

1. Read ``logistics.amazon.co.uk`` session cookies from the user's Firefox
   profile (Midway/Federate — ``logistics-federate-token`` + friends).
2. GET a station dashboard page (``/station/dashboard/search`` by default)
   as HTML.
3. Scrape the server-minted ``boson.apiUsageKey`` embedded in that page::

       window.sessionStorage.setItem('boson.apiUsageKey',
           "scc-boson-api-...:<epoch_ms>:<sig>");

4. POST to the gateway with that value in the ``X-API-Usage-Key`` header
   (plus cookies and ``X-Requested-With: XMLHttpRequest``).

The ``apiUsageKey`` embeds a timestamp and is minted per page load, so we
scrape it fresh for every :class:`SccClient` and never cache it to disk.

Public surface:

* :class:`SccAuthError` / :class:`SccClientError`
* :func:`cookies_status`
* :func:`resolve_node_id`
* :class:`SccClient` — ``proxy_call`` + typed helpers.
* :func:`fetch_package_states` — convenience ``{tracking_id: state}`` map.
"""
from __future__ import annotations

import math
import os
import re
import threading as _threading
import time
from typing import Any, Callable, Iterable

import requests

from app.core.amounts import scc_amount_to_eur as _amount_to_eur
from app.integrations.firefox_cookies import (
    LOGISTICS_HOST_PATTERNS,
    has_required,
    logistics_cookies_from_profile,
    session_status,
)

# SCC on ``logistics.amazon.co.uk`` authenticates via Midway. The browser
# sends ``amzn_sso_token`` (the Midway JWT) + ``amzn_sso_rfp`` plus the retail
# ``session-id``/``session-token`` pair; the JWT is the one we read from
# Firefox and it MUST be currently valid to load the dashboard page (from
# which we scrape the ``apiUsageKey``).
#
# ``amzn_sso_token`` is short-lived: once it expires the dashboard GET is
# 302-redirected to the SSO login (which only the browser can complete via its
# Midway cookies), so an expired token means the operator must reload SCC in
# Firefox. The cookie reader already drops expired cookies, so gating on this
# single token correctly reports "not ready" the moment it lapses — do NOT add
# the long-lived ``logistics-federate-token`` here: on its own it still bounces
# to SSO, so accepting it would report a live session that then fails.
SCC_REQUIRED_COOKIES = ("amzn_sso_token",)

SCC_HOST = "logistics.amazon.co.uk"
SCC_BASE_URL = f"https://{SCC_HOST}"
SCC_PROXY_PATH = "/station/proxyapigateway/data"
# Any station dashboard page embeds ``boson.apiUsageKey``. ``search`` is the
# lightest one and is where the browser signs batch tracking-ID lookups.
SCC_DASHBOARD_PATH = "/station/dashboard/search"
SCC_LOGIN_URL = SCC_BASE_URL + SCC_DASHBOARD_PATH
SCC_DEFAULT_TIMEOUT = 40.0
# Batch size per ``batchGetPackageSummary`` request. The 4,999 figure is the
# limit of the dashboard SEARCH TEXTAREA (a client-side split), NOT the API's:
# raising this constant to 4_999 (2026-09-05) broke every live SCC lookup —
# HUB Intentos / HUB Dropped / HUB Devoluciones failed with ``error.scc_failed``
# on plans of a few thousand IDs (user-reported 2026-09-08). The proxy gateway
# rejects/fails oversized identifier lists, so keep batches modest: a single
# failure doesn't poison a large lookup and we stay under the body cap. Large
# HUB plans therefore run as multiple SCC passes; retry each pass because a
# transient SCC failure must not discard an otherwise valid analysis.
# DO NOT raise this again without verifying a full-size live request first.
SCC_BATCH_SIZE = 100
SCC_BATCH_MAX_ATTEMPTS = 3
SCC_BATCH_RETRY_DELAY_S = 0.75
# ``getDrillDownData`` enforces a server-side page cap of 100 rows even when
# the request asks for more. Page explicitly so On Road consumers receive the
# complete result set instead of silently stopping at the first 100 packages.
SCC_DRILLDOWN_PAGE_SIZE = 100

# SCC ageing "packageStatus" -> [direction] map that makes up the "On Road"
# export the First Attempt tool consumes: packages currently *on the way*
# (In Transit DS -> Customer) plus the ones that came back as a failed /
# rejected attempt. "In Transit" carries the No-Reason / Sin-Intento bucket
# and "Delivery Failed" carries the failure reasons (First Attempt / Business
# Closed / UTA / UTL). Passed straight as the drill-down ``packageStatusMap``.
ONROAD_STATUS_MAP: dict[str, list[str]] = {
    "In Transit": ["DS -> Customer"],
    "Delivery Failed": [],
    "Delivery Rejected": [],
}
# The same "On Road" set split in two drill-downs (see fetch_onroad_combined):
# the failed/rejected one is small and always complete; In Transit can exceed
# the 10 000-row page budget on its own.
ONROAD_FAILED_STATUS_MAP: dict[str, list[str]] = {
    "Delivery Failed": [],
    "Delivery Rejected": [],
}
ONROAD_TRANSIT_STATUS_MAP: dict[str, list[str]] = {
    "In Transit": ["DS -> Customer"],
}

# The other SCC "On Road" section — the "Iniciar / In Transit" view: packages
# received / inducted / marked-for-reprocess at the station within the 0-1 day
# dwell window (i.e. the ones still to be initiated / in transit at station).
# Captured from ``getDrillDownData`` with this status map + DWELL_TIME filter.
INICIAR_STATUS_MAP: dict[str, list[str]] = {
    "Received": ["DS -> Customer"],
    "Inducted": [],
    "Marked for Reprocess": [],
}
INICIAR_DWELL: tuple[str, ...] = ("0_DAY", "1_DAY")

# EAD / At Station "Induct" section == the "Iniciar / In Transit" view above
# (Received / Inducted / Marked for Reprocess within the 0-1 day dwell window).
# Aliased so the EAD tool reads intent-named constants.
INDUCT_STATUS_MAP: dict[str, list[str]] = INICIAR_STATUS_MAP
INDUCT_DWELL: tuple[str, ...] = INICIAR_DWELL

# EAD / At Station "Retained" section — the packages retained at the station
# for a reason (held / problem solve / sidelined / rescheduled / ...). Captured
# from the SCC "retained" dashboard export (getDrillDownData): one drill-down
# per category, all sharing the 0-1 day dwell + Delivery shipment filter. The
# browser posts each category separately (so it can paginate), which is also
# how we tag each returned row with the category it belongs to.
RETAINED_STATUS_MAP: dict[str, list[str]] = {
    "FDD": [],
    "Wrong Cycle Induct": [],
    "Sidelined Closed": [],
    "Pending Customer Feedback": [],
    "Staged": ["DS -> DS"],
    "Problem Solve": [],
    "Rescheduled": [],
    "Customer Notes Updated": [],
}
RETAINED_DWELL: tuple[str, ...] = ("0_DAY", "1_DAY")

# "Delivery Cancelled" drill-down consumed by the Duplicados y Cancelados
# tool. Captured from the SCC ``overviewv2`` dashboard export: the
# ``packageStatusMap`` is just ``{"Delivery Cancelled": []}`` and the only
# term filter is a ``DWELL_TIME`` minute range (NO ``SHIPMENT_TYPE`` — the
# dashboard omits it for this tile, and adding one returns 0 rows). The
# upper bound is a rolling window the dashboard computes per page load; we
# default to a 24h window so we capture the full operational day and let
# callers widen/narrow it via ``dwell_max_minutes``.
CANCELLED_STATUS_MAP: dict[str, list[str]] = {"Delivery Cancelled": []}
CANCELLED_DWELL_MAX_MINUTES = 1440

# SCC "Dwell" dashboard (``/station/dashboard/dwell``, DWELLING_VIEW) — the
# tiles the operator drills into to audit packages physically dwelling at the
# station and spot stow errors: Inducted (sorted but not yet stowed), Stow
# Problem Solve and Stow Buffered. The Dwell / Errors tool consumes exactly
# this set. Captured from the dwell dashboard's ``getDrillDownData`` calls,
# which — unlike the ageing views — filter by a ``lastUpdatedRange`` window
# (epoch seconds) instead of a ``DWELL_TIME`` term.
DWELL_ERRORS_STATUS_MAP: dict[str, list[str]] = {
    "Inducted": [],
    "Stow Problem Solve": [],
    "Stow Buffered": [],
}
# How far back / forward the dwell drill-down ``lastUpdatedRange`` reaches by
# default. A rolling 24h look-back captures the full operational day; the
# small look-ahead absorbs clock skew between the browser and the gateway.
DWELL_DEFAULT_LOOKBACK_HOURS = 24
DWELL_DEFAULT_LOOKAHEAD_HOURS = 24

# SCC dwell "Salida pendiente / Pending Depart" row — the dashboard groups
# under that label the packages that finished sort and are waiting to leave
# the station. Captured from the dwell dashboard HAR (26-08-03): clicking the
# row posts a single ``getDrillDownData`` with exactly this combined
# ``packageStatusMap`` (Stowed and Staged carry the ``DS -> Customer``
# direction; Picked and Pick Problem Solve have none) through the dwell
# envelope (``lastUpdatedRange``). There is NO single "Pending Depart"
# packageStatus in the gateway — the row is a UI grouping. The Sin Secuencia
# tool keeps only the packages with NO route assigned whose ``source`` is our
# own station, i.e. the ones missing a sequence.
PENDING_DEPART_STATUS_MAP: dict[str, list[str]] = {
    "Stowed": ["DS -> Customer"],
    "Picked": [],
    "Staged": ["DS -> Customer"],
    "Pick Problem Solve": [],
}

# The drill-down returns ``state`` as an enum (DEPARTED, DELIVERY_ATTEMPTED,
# ...), not the human label the SCC "On Road" CSV carries. First Attempt's
# ``analyze`` keys off ``State == "Delivery Failed"`` (for PHR) and treats
# in-transit / no-reason packages as "Sin Intento", so we normalise the enum
# to the label the tool expects. Unmapped states fall through unchanged.
_ONROAD_STATE_LABELS: dict[str, str] = {
    "DEPARTED": "In Transit",
    "IN_TRANSIT": "In Transit",
    "IN_TRANSIT_TO_STORE": "In Transit",
    "OUT_FOR_DELIVERY": "In Transit",
    "DELIVERY_ATTEMPTED": "Delivery Failed",
    "UNSUCCESSFUL_AT_STORE": "Delivery Failed",
    "UNDELIVERABLE": "Delivery Failed",
    "DELIVERY_FAILED": "Delivery Failed",
    "REJECTED": "Delivery Rejected",
    "DELIVERY_REJECTED": "Delivery Rejected",
    # "Iniciar / In Transit" section (received / inducted at station).
    "RECEIVED": "In Transit",
    "INDUCTED": "In Transit",
    "DELAYED_AT_STATION": "In Transit",
    "MARKED_FOR_REPROCESS": "In Transit",
}

# Search (bulk lookup) state labels. Here "Received" means the package was
# received back at the station = a RETURN, so it must stay "Received" (unlike
# the On Road map above). Mapped to the states Cierre's returns reader expects.
_SEARCH_STATE_LABELS: dict[str, str] = {
    "RECEIVED": "Received",
    "DELIVERY_FAILED": "Delivery Failed",
    "DELIVERY_REJECTED": "Delivery Rejected",
    "IN_TRANSIT_TO_CUSTOMER": "In Transit",
    "IN_TRANSIT": "In Transit",
    "HELD": "Held",
    "MARKED_FOR_REPROCESS": "Marked for Reprocess",
}

# SCC "receive" dashboard line-haul list (``/ivs/getNodeLineHaulList``) — the
# inbound trucks the receive board shows. The raw ``lineHaulStatus`` enum is
# mapped to the label the Flow Meeting NS Camiones tables classify on (see the
# front-end ``statusOptions`` / ``llegadoStatuses``): ``ARRIVED`` becomes a
# "llegado" truck, ``IN_TRANSIT`` / ``PENDING_DEPART`` an "entrante".
_LINEHAUL_STATUS_LABELS: dict[str, str] = {
    "ARRIVED": "Arrived",
    "IN_TRANSIT": "In-Transit",
    "IN_TRANSIT_FROM_UPSTREAM": "In-Transit",
    "PENDING_DEPART": "Pending Depart",
    "PENDING_DEPART_FROM_UPSTREAM": "Pending Depart",
    "DEPARTED": "In-Transit",
    "CANCELLED": "Canceled",
    "CANCELED": "Canceled",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# Fields the SCC "search" bulk lookup requests for each package.
_DEFAULT_SUMMARY_FIELDS = (
    "ESTIMATED_ARRIVAL_DATE",
    "SCHEDULED_DELIVERY_TIME",
    "SHIP_METHOD",
    "PROVIDER_ID",
    "SHIP_DATE",
    "SHIP_OPTION",
    "PROMISED_DELIVERY_DATE",
)

# ``window.sessionStorage.setItem('boson.apiUsageKey', "….")`` — tolerant to
# single/double quotes and whitespace around the arguments.
_API_USAGE_KEY_RE = re.compile(
    r"""boson\.apiUsageKey['"]\s*,\s*['"]([^'"]+)['"]"""
)
_NODE_ID_RE = re.compile(r"""boson\.nodeId['"]\s*,\s*['"]([^'"]+)['"]""")
_PROXY_ALLOWED_RE = re.compile(
    r"""boson\.isBosonProxyAllowed['"]\s*,\s*(true|false)""", re.IGNORECASE
)


class SccAuthError(RuntimeError):
    """Raised when cookies / apiUsageKey are missing or rejected (login)."""


class SccClientError(RuntimeError):
    """Raised on any other unexpected SCC response."""


# ---------------------------------------------------------------------------
# Cookies / login status
# ---------------------------------------------------------------------------


def _read_cookies() -> dict[str, str]:
    try:
        return logistics_cookies_from_profile(target_host=SCC_HOST)
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Auto-refresh del ``amzn_sso_token`` (mint server-side vía Midway)
# ---------------------------------------------------------------------------
#
# El ``amzn_sso_token`` caduca mucho antes que la sesión Midway (~20 h). El
# navegador lo renueva solo al recargar SCC (302 → SSO → 302 de vuelta con
# token fresco); aquí replicamos esa navegación con las cookies Midway del
# perfil de Firefox — el mismo patrón ya validado en Mercury, Siphon y el
# cliente Caravan. Así "extracción automática" funciona sin que el operador
# tenga que recargar SCC cada vez que el token expira; solo cuando Midway
# también está caducado toca el login manual de siempre.

_MINT_LOCK = _threading.Lock()
# cookies: último jar acuñado · exp: ``exp`` del JWT · fail_until: backoff
_MINTED: dict[str, Any] = {"cookies": None, "exp": 0.0, "fail_until": 0.0}
_MINT_FAIL_BACKOFF_S = 60.0


def _jwt_exp(token: str) -> float:
    """Best-effort ``exp`` (epoch) de un JWT; 0.0 si no se puede parsear."""
    try:
        import base64
        import json as _json

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = _json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return float(data.get("exp") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _mint_sso_cookies(timeout: float = 25.0) -> dict[str, str] | None:
    """GET al dashboard con la cookie ``session`` de Midway siguiendo la
    cadena SSO. Devuelve las cookies del jar (incluye ``amzn_sso_token``
    fresco) si la página termina en 200 con ``apiUsageKey``; ``None`` si
    Midway también está caducado o la red falla."""
    try:
        from app.integrations.firefox_cookies import midway_cookies_from_profile

        midway = midway_cookies_from_profile() or {}
    except Exception:  # noqa: BLE001
        midway = {}
    if not midway.get("session"):
        return None

    sess = requests.Session()
    try:
        for name, value in midway.items():
            for domain in ("midway-auth.amazon.com", ".amazon.com"):
                try:
                    sess.cookies.set(name, value, domain=domain)
                except Exception:  # noqa: BLE001
                    pass
        # Las cookies logistics de larga vida del perfil (federate-token,
        # session-id…) ayudan a que la vuelta del SSO aterrice sin más saltos.
        for name, value in (_read_cookies() or {}).items():
            if name in SCC_REQUIRED_COOKIES:
                continue
            try:
                sess.cookies.set(name, value, domain=SCC_HOST)
            except Exception:  # noqa: BLE001
                pass
        resp = sess.get(
            SCC_BASE_URL + SCC_DASHBOARD_PATH,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,*/*",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        from urllib.parse import urlparse

        final_host = (urlparse(resp.url).hostname or "").lower()
        if resp.status_code != 200 or "midway" in final_host:
            return None
        if not _scrape_api_usage_key(resp.text or ""):
            return None
        minted = {
            c.name: c.value
            for c in sess.cookies
            if (c.domain or "").lstrip(".").endswith("amazon.co.uk") and c.value
        }
        if not minted.get("amzn_sso_token"):
            return None
        return minted
    except requests.RequestException:
        return None
    finally:
        sess.close()


def _minted_cookies(force: bool = False) -> dict[str, str] | None:
    """Mint cacheado: reutiliza el jar hasta ~1 min antes del ``exp`` del
    JWT; tras un fallo espera ``_MINT_FAIL_BACKOFF_S`` antes de reintentar
    (el status del navbar sondea cada 60 s y no debe martillear Midway)."""
    now = time.time()
    with _MINT_LOCK:
        cached = _MINTED.get("cookies")
        if not force and cached and now < float(_MINTED.get("exp") or 0.0) - 60:
            return dict(cached)
        if not force and now < float(_MINTED.get("fail_until") or 0.0):
            return None
        minted = _mint_sso_cookies()
        if minted:
            exp = _jwt_exp(minted.get("amzn_sso_token", "")) or (now + 600.0)
            _MINTED.update({"cookies": dict(minted), "exp": exp, "fail_until": 0.0})
            return dict(minted)
        _MINTED.update({"cookies": None, "exp": 0.0, "fail_until": now + _MINT_FAIL_BACKOFF_S})
        return None


def _effective_cookies() -> dict[str, str]:
    """Cookies del perfil, auto-refrescadas vía Midway si el token caducó."""
    cookies = _read_cookies()
    if has_required(cookies, SCC_REQUIRED_COOKIES):
        return cookies
    minted = _minted_cookies()
    if minted:
        merged = dict(cookies)
        merged.update(minted)
        return merged
    return cookies


def _probe_session(cookies: dict[str, str]) -> str:
    """Live check: does SCC actually serve the ``apiUsageKey`` to these cookies?

    Runs the exact step every extraction starts with (``SccClient._ensure_key``:
    GET the dashboard, scrape ``boson.apiUsageKey``). A cookie can look valid
    on disk (present, JWT ``exp`` in the future) while the session was
    invalidated server-side — in which case the dashboard redirects to SSO and
    every extraction dies with :class:`SccAuthError`.
    """
    client = SccClient(cookies=cookies, timeout=15.0)
    try:
        client._ensure_key()
        return "ok"
    except SccAuthError:
        return "auth"
    except SccClientError:
        return "error"


def cookies_status() -> dict[str, Any]:
    """Return ``{ready, missing, expired, token_expired, cookie_count, login_url}``.

    "ready" requires a currently-valid ``amzn_sso_token`` — that Midway JWT must
    be live to load the dashboard page we scrape the ``apiUsageKey`` from.
    ``expired`` distinguishes a lapsed session (token present but past its
    ``exp``) from one that was never established, so the UI can prompt for a
    *re-login* instead of the misleading "falta el token".

    The on-disk check alone used to paint the chip "Conectado" while every
    extraction failed: a session can be rejected server-side with the cookies
    still sitting in the profile. When the cookies look usable we now confirm
    against SCC itself (TTL-cached, see
    :mod:`app.integrations.live_session_probe`); a server-side rejection
    reports ``ready: False`` + ``token_expired: True`` so the UI runs its
    normal re-login flow. An inconclusive probe (network blip, SCC 5xx) keeps
    the passive verdict so the chip doesn't flap.
    """
    from app.integrations.live_session_probe import cached_probe, cookie_fingerprint

    status = session_status(
        LOGISTICS_HOST_PATTERNS,
        SCC_REQUIRED_COOKIES,
        target_host=SCC_HOST,
    )
    ready = bool(status["ready"])
    token_expired = "amzn_sso_token" in status["expired"]
    session_rejected = False
    if ready:
        cookies = _read_cookies()
        outcome = cached_probe(
            "scc",
            cookie_fingerprint(*(cookies.get(name) for name in SCC_REQUIRED_COOKIES)),
            lambda: _probe_session(cookies),
        )
        if outcome == "auth":
            ready = False
            session_rejected = True
            token_expired = True
    auto_minted = False
    if not ready:
        # Token caducado/rechazado pero Midway puede seguir vivo: intenta el
        # mint server-side. Si sale, la sesión ESTÁ lista (el mint ya validó
        # el ``apiUsageKey`` contra SCC — es una sonda en vivo en sí mismo).
        if _minted_cookies():
            ready = True
            token_expired = False
            session_rejected = False
            auto_minted = True
    return {
        "ready": ready,
        "missing": status["missing"],
        "expired": status["expired"],
        "token_expired": token_expired,
        "session_rejected": session_rejected,
        "auto_minted": auto_minted,
        "cookie_count": status["cookie_count"],
        "login_url": SCC_LOGIN_URL,
    }


def resolve_node_id(node_id: str | None = None) -> str:
    """Return the SCC ``nodeId`` (station code) to query.

    Prefers an explicit value, then the navbar selection, then the
    configured ``STATION_CODE``.
    """
    explicit = (node_id or "").strip().upper()
    if explicit:
        return explicit
    try:
        from app.core.station_context import resolve_station_code

        resolved = resolve_station_code(
            default=(os.environ.get("STATION_CODE", "") or "").strip().upper()
        )
        if resolved:
            return resolved
    except Exception:  # noqa: BLE001
        pass
    return (os.environ.get("STATION_CODE", "") or "").strip().upper()


def _scrape_api_usage_key(html_text: str) -> str | None:
    match = _API_USAGE_KEY_RE.search(html_text or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SccClient:
    """Authenticated SCC proxy-gateway client (one scraped key per instance).

    Construct once per operation and reuse for several ``proxy_call`` /
    helper calls: the ``apiUsageKey`` is scraped lazily on the first call
    and reused for the instance's lifetime (the browser reuses it for the
    whole page session too).
    """

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        node_id: str | None = None,
        timeout: float = SCC_DEFAULT_TIMEOUT,
        dashboard_path: str = SCC_DASHBOARD_PATH,
    ) -> None:
        self._cookies = cookies if cookies is not None else _effective_cookies()
        self.node_id = resolve_node_id(node_id)
        self.timeout = timeout
        self._dashboard_path = dashboard_path
        self._session = requests.Session()
        if self._cookies:
            self._session.cookies.update(self._cookies)
        self._api_usage_key: str | None = None
        self._page_url = f"{SCC_BASE_URL}{dashboard_path}"

    # -- auth -------------------------------------------------------------

    def _record_auth_reject(self) -> None:
        """Remember a server-side rejection so ``cookies_status`` sees it.

        Without this, a 401 raised mid-extraction could race a still-fresh
        "ok" entry in the live-probe cache and the chip would flip back to
        "Conectado" right after the failure.
        """
        try:
            from app.integrations.live_session_probe import (
                cookie_fingerprint,
                record_outcome,
            )

            record_outcome(
                "scc",
                cookie_fingerprint(
                    *(self._cookies.get(name) for name in SCC_REQUIRED_COOKIES)
                ),
                "auth",
            )
        except Exception:  # noqa: BLE001 - best-effort cache seed
            pass

    def _apply_cookies(self, cookies: dict[str, str]) -> None:
        self._cookies = dict(cookies)
        self._session.cookies.clear()
        self._session.cookies.update(self._cookies)

    def _fetch_key_once(self) -> tuple[str | None, Exception | None]:
        """Un GET al dashboard + scrape del ``apiUsageKey``.

        Devuelve ``(key, None)`` o ``(None, excepción)`` — auth
        (:class:`SccAuthError`) o transporte (:class:`SccClientError`) —
        SIN registrar el rechazo: eso lo decide ``_ensure_key`` tras agotar
        el fallback de mint.
        """
        try:
            page = self._session.get(
                self._page_url,
                headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            return None, SccClientError(f"Error SSL contra {SCC_HOST}: {exc}")
        except (OSError, requests.RequestException) as exc:
            return None, SccClientError(f"Error de red contra {SCC_HOST}: {exc}")

        if page.status_code in (301, 302, 303, 307, 308, 401, 403):
            return None, SccAuthError(
                f"SCC redirige/rechaza la pagina (HTTP {page.status_code}). "
                "Recarga SCC en Firefox e inicia sesion."
            )
        if page.status_code != 200:
            return None, SccClientError(
                f"SCC devolvio HTTP {page.status_code} al cargar el dashboard."
            )

        key = _scrape_api_usage_key(page.text or "")
        if not key:
            return None, SccAuthError(
                "No se encontro boson.apiUsageKey en SCC (sesion caducada). "
                "Recarga SCC en Firefox e inicia sesion."
            )
        return key, None

    def _ensure_key(self) -> str:
        if self._api_usage_key:
            return self._api_usage_key

        failure: Exception | None = None
        if has_required(self._cookies, SCC_REQUIRED_COOKIES):
            key, failure = self._fetch_key_once()
            if key:
                self._api_usage_key = key
                return key
            # Los fallos de transporte no son de sesión: el mint no ayuda.
            if not isinstance(failure, SccAuthError):
                raise failure

        # Token ausente/caducado/rechazado: intenta renovarlo vía Midway
        # (``force``: el token rechazado puede SER el mint cacheado).
        minted = _minted_cookies(force=True)
        if minted:
            merged = dict(self._cookies)
            merged.update(minted)
            self._apply_cookies(merged)
            key, retry_failure = self._fetch_key_once()
            if key:
                self._api_usage_key = key
                return key
            failure = retry_failure or failure

        # Solo se registra el rechazo cuando el fallo final es de sesión
        # (un error de transporte del reintento no debe pintar el chip rojo).
        if failure is None or isinstance(failure, SccAuthError):
            self._record_auth_reject()
        if isinstance(failure, Exception):
            raise failure
        raise SccAuthError(
            "Sesión de SCC caducada (amzn_sso_token). "
            "Abre/recarga SCC en Firefox e inicia sesión, luego reintenta."
        )

    # -- core call --------------------------------------------------------

    def proxy_call(
        self,
        resource_path: str,
        process_name: str,
        *,
        http_method: str = "post",
        request_body: Any | None = None,
        request_params: Any | None = None,
    ) -> Any:
        """POST one operation through the proxy gateway and return the JSON.

        ``request_body`` is used for downstream POSTs; ``request_params`` for
        downstream GETs (mirrors the web app's envelope). Raises
        :class:`SccAuthError` when login is required and
        :class:`SccClientError` otherwise.
        """
        key = self._ensure_key()
        envelope: dict[str, Any] = {
            "resourcePath": resource_path,
            "httpMethod": http_method,
            "processName": process_name,
        }
        if request_body is not None:
            envelope["requestBody"] = request_body
        if request_params is not None:
            envelope["requestParams"] = request_params

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "X-API-Usage-Key": key,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": SCC_BASE_URL,
            "Referer": self._page_url,
        }
        try:
            resp = self._session.post(
                f"{SCC_BASE_URL}{SCC_PROXY_PATH}",
                json=envelope,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            raise SccClientError(f"Error SSL contra SCC API: {exc}") from exc
        except (OSError, requests.RequestException) as exc:
            raise SccClientError(f"Error de red contra SCC API: {exc}") from exc

        if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
            self._record_auth_reject()
            raise SccAuthError(
                f"SCC rechazo la peticion ({resp.status_code}). "
                "Recarga SCC en Firefox e intenta de nuevo."
            )
        if resp.status_code != 200:
            raise SccClientError(
                f"SCC API devolvio HTTP {resp.status_code}. "
                f"{(resp.text or '')[:200]}".strip()
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise SccClientError(f"SCC API no devolvio JSON valido: {exc}") from exc

    # -- typed helpers ----------------------------------------------------

    def batch_get_package_summary(
        self,
        tracking_ids: Iterable[str],
        *,
        node_id: str | None = None,
        include_fields: Iterable[str] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the ``packageSummaryList`` for the given tracking IDs.

        Chunks the lookup into :data:`SCC_BATCH_SIZE` batches and
        concatenates the results. ``progress`` (optional) is invoked with
        ``(ids_done, ids_total)`` before the first batch and after each
        one, so long lookups can surface "searching 1.200/6.440" to the
        UI; a failing callback is swallowed — reporting progress must
        never break the lookup it describes.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        fields = list(include_fields or _DEFAULT_SUMMARY_FIELDS)

        # De-dupe while preserving order.
        seen: set[str] = set()
        ids: list[str] = []
        for raw in tracking_ids:
            tid = str(raw or "").strip()
            if tid and tid.upper() not in seen:
                seen.add(tid.upper())
                ids.append(tid)

        def _report(done: int) -> None:
            if progress is None:
                return
            try:
                progress(done, len(ids))
            except Exception:  # noqa: BLE001 - best-effort by contract.
                pass

        out: list[dict[str, Any]] = []
        _report(0)
        for start in range(0, len(ids), SCC_BATCH_SIZE):
            chunk = ids[start : start + SCC_BATCH_SIZE]
            for attempt in range(SCC_BATCH_MAX_ATTEMPTS):
                try:
                    data = self.proxy_call(
                        "/os/batchGetPackageSummary",
                        "oculus",
                        http_method="post",
                        request_body={
                            "idType": "TRACKING_ID",
                            "identifiers": chunk,
                            "nodeId": node,
                            "includeFields": fields,
                        },
                    )
                    break
                except SccClientError:
                    if attempt + 1 >= SCC_BATCH_MAX_ATTEMPTS:
                        raise
                    time.sleep(SCC_BATCH_RETRY_DELAY_S * (attempt + 1))
            if isinstance(data, dict):
                out.extend(data.get("packageSummaryList") or [])
            _report(min(start + len(chunk), len(ids)))
        return out

    def get_dwelling_package_data(
        self,
        *,
        node_id: str | None = None,
        view: str = "AGEING_VIEW",
        shipment_types: Iterable[str] = ("Delivery",),
        leg: str = "FORWARD",
    ) -> list[dict[str, Any]]:
        """Return the dwell/ageing summary rows (``metricResult``)."""
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        data = self.proxy_call(
            "/os/getDwellingPackageData",
            "oculus",
            http_method="post",
            request_body={
                "nodeId": node,
                "view": view,
                "filters": _build_filters({"SHIPMENT_TYPE": list(shipment_types)}),
                "leg": leg,
            },
        )
        if isinstance(data, dict):
            return data.get("metricResult") or []
        return []

    def get_drilldown_data(
        self,
        package_status_map: dict[str, list[str]],
        *,
        node_id: str | None = None,
        dwell_time: Iterable[str] | None = None,
        shipment_types: Iterable[str] = ("Delivery",),
        size: int = 10000,
        starting_index: int = 0,
    ) -> list[dict[str, Any]]:
        """Return up to ``size`` package-level rows, paging the SCC view.

        SCC caps each ``getDrillDownData`` response at 100 rows regardless
        of the requested size. Advance by the number actually returned so a
        live view with more than 100 packages is not silently truncated.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        term: dict[str, list[str]] = {"SHIPMENT_TYPE": list(shipment_types)}
        if dwell_time:
            term["DWELL_TIME"] = list(dwell_time)

        try:
            max_rows = max(0, int(size))
            next_index = max(0, int(starting_index))
        except (TypeError, ValueError):
            max_rows = 0
            next_index = 0
        if max_rows == 0:
            return []

        rows: list[dict[str, Any]] = []
        previous_marker: tuple[str, ...] | None = None
        while len(rows) < max_rows:
            page_size = min(SCC_DRILLDOWN_PAGE_SIZE, max_rows - len(rows))
            data = self.proxy_call(
                "/os/getDrillDownData",
                "oculus",
                http_method="post",
                request_body={
                    "nodeId": node,
                    "packageStatusMap": package_status_map,
                    "filters": _build_filters(term),
                    "size": page_size,
                    "startingIndex": next_index,
                },
            )
            page = data.get("packageResultList") or [] if isinstance(data, dict) else []
            if not page:
                break

            # Fail visibly rather than returning a deceptive first page if a
            # future SCC response stops honoring ``startingIndex``.
            marker = tuple(repr(row) for row in page)
            if marker == previous_marker:
                raise SccClientError(
                    "SCC devolvió repetida la misma página de resultados."
                )
            previous_marker = marker

            remaining = max_rows - len(rows)
            rows.extend(page[:remaining])
            received = len(page)
            if received < page_size:
                break
            next_index += received

        return rows

    def get_cancelled_drilldown(
        self,
        *,
        node_id: str | None = None,
        dwell_max_minutes: int | None = CANCELLED_DWELL_MAX_MINUTES,
        size: int = 10000,
    ) -> list[dict[str, Any]]:
        """Return the raw ``packageResultList`` for the "Delivery Cancelled" tile.

        Reproduces the SCC ``overviewv2`` "Delivery Cancelled" drill-down
        exactly as the browser sends it: ``packageStatusMap`` with the
        single ``"Delivery Cancelled"`` key and a ``DWELL_TIME`` window of
        ``["0_MINUTES", "<dwell_max_minutes>_MINUTES"]``. No
        ``SHIPMENT_TYPE`` term is added (the dashboard omits it here — adding
        one filters everything out). Pass ``dwell_max_minutes=None`` to drop
        the dwell filter entirely and return every cancelled package.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        term: dict[str, list[str]] = {}
        if dwell_max_minutes and int(dwell_max_minutes) > 0:
            term["DWELL_TIME"] = ["0_MINUTES", f"{int(dwell_max_minutes)}_MINUTES"]
        data = self.proxy_call(
            "/os/getDrillDownData",
            "oculus",
            http_method="post",
            request_body={
                "nodeId": node,
                "packageStatusMap": CANCELLED_STATUS_MAP,
                "filters": _build_filters(term),
                "size": size,
                "startingIndex": 0,
            },
        )
        if isinstance(data, dict):
            return data.get("packageResultList") or []
        return []

    def get_dwell_drilldown(
        self,
        package_status_map: dict[str, list[str]] | None = None,
        *,
        node_id: str | None = None,
        shipment_types: Iterable[str] = ("Delivery",),
        cycles: Iterable[str] | None = None,
        lookback_hours: int = DWELL_DEFAULT_LOOKBACK_HOURS,
        lookahead_hours: int = DWELL_DEFAULT_LOOKAHEAD_HOURS,
        size: int = 10000,
        starting_index: int = 0,
    ) -> list[dict[str, Any]]:
        """Return package-level rows for the SCC "Dwell" dashboard tiles.

        Reproduces the dwell dashboard's ``getDrillDownData`` envelope
        exactly: a ``packageStatusMap`` of the dwelling tiles plus a
        ``lastUpdatedRange`` window (epoch seconds) instead of the
        ``DWELL_TIME`` term the ageing views use. ``cycles`` optionally
        narrows to specific MRC cycles (e.g. ``("CYCLE_1",)``); omitted, all
        cycles are returned.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        status_map = package_status_map or DWELL_ERRORS_STATUS_MAP
        term: dict[str, list[str]] = {"SHIPMENT_TYPE": list(shipment_types)}
        if cycles:
            term["CYCLE"] = list(cycles)
        now = int(time.time())
        data = self.proxy_call(
            "/os/getDrillDownData",
            "oculus",
            http_method="post",
            request_body={
                "nodeId": node,
                "packageStatusMap": status_map,
                "filters": _build_filters(term),
                "lastUpdatedRange": {
                    "startTime": now - int(lookback_hours) * 3600,
                    "endTime": now + int(lookahead_hours) * 3600,
                },
                "size": size,
                "startingIndex": starting_index,
            },
        )
        if isinstance(data, dict):
            return data.get("packageResultList") or []
        return []

    def fetch_dwell_error_rows(
        self,
        *,
        node_id: str | None = None,
        status_map: dict[str, list[str]] | None = None,
        cycles: Iterable[str] | None = None,
        lookback_hours: int = DWELL_DEFAULT_LOOKBACK_HOURS,
    ) -> list[dict[str, Any]]:
        """Return the dwelling packages for the SCC "Dwell" tiles, deduped.

        Sends a single ``getDrillDownData`` with the full ``packageStatusMap``
        (exactly like the dashboard's combined call in the HAR) so each
        package comes back once with its real ``state`` enum. Querying one
        tile at a time makes the gateway return overlapping packages, so we
        also de-dupe by ``trackingId`` as a safety net. Rows keep their raw
        SCC fields (``trackingId`` / ``state`` / ``lastUpdatedTime`` /
        ``length`` / ``sortZone`` / ...) for the tool to map to its columns.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        smap = status_map or DWELL_ERRORS_STATUS_MAP
        rows = self.get_dwell_drilldown(
            smap,
            node_id=node,
            cycles=cycles,
            lookback_hours=lookback_hours,
        )
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows:
            tid = str(r.get("trackingId") or "").strip().upper()
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            out.append(dict(r))
        return out

    def fetch_pending_depart_rows(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] | None = None,
        lookback_hours: int = DWELL_DEFAULT_LOOKBACK_HOURS,
        only_without_route: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the dwell "Salida pendiente / Pending Depart" packages.

        Drills the statuses grouped under the dashboard's "Salida pendiente"
        row (:data:`PENDING_DEPART_STATUS_MAP`) exactly like the browser does
        (single combined ``getDrillDownData`` through the dwell envelope, per
        the 26-08-03 HAR) and, by default, keeps only the **customer packages
        without an assigned route whose** ``source`` **is our own station** —
        the "sin secuencia" set. Two kinds of freight never get a delivery
        route here and are dropped from that set:

        * packages whose ``source`` is another station (re-plans routed away;
          their ``sortZone`` is the other station's code, not a real zone),
        * return-to-FC freight (``MIDDLE_MILE_NODE`` destination or
          ``*RETURNS*`` ship method, e.g. CRE linehaul bags).

        Rows keep their raw SCC fields (``trackingId`` / ``sortZone`` /
        ``routeCode`` / ``lastScanLocation`` / ``lastScanBy`` /
        ``lastUpdatedTime`` / ...) and are deduped by ``trackingId``.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        rows = self.get_dwell_drilldown(
            PENDING_DEPART_STATUS_MAP,
            node_id=node,
            cycles=cycles,
            lookback_hours=lookback_hours,
        )
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows:
            tid = str(r.get("trackingId") or "").strip().upper()
            if not tid or tid in seen:
                continue
            if only_without_route:
                route = str(
                    r.get("routeCode") or r.get("currentRouteCode") or ""
                ).strip()
                if route and route.lower() not in {"nan", "none", "null"}:
                    continue
                source = str(r.get("source") or "").strip().upper()
                if source and source != node:
                    continue
                destination = str(r.get("destination") or "").strip().upper()
                ship_method = str(r.get("shipMethod") or "").strip().upper()
                if destination == "MIDDLE_MILE_NODE" or "RETURN" in ship_method:
                    continue
            seen.add(tid)
            out.append(dict(r))
        return out

    def fetch_categorized_drilldown(
        self,
        status_map: dict[str, list[str]],
        *,
        node_id: str | None = None,
        dwell_time: Iterable[str] | None = None,
        shipment_types: Iterable[str] = ("Delivery",),
        size: int = 10000,
    ) -> list[dict[str, Any]]:
        """Drill-down one category at a time, tagging each row with it.

        The SCC dashboard posts a separate ``getDrillDownData`` per
        ``packageStatusMap`` key so it can paginate each tile; we do the
        same so every returned package carries the ``category`` (the tile /
        status label) it belongs to. Rows keep their raw SCC fields
        (``trackingId`` / ``state`` / ``reason`` / ``internalEAD`` / ...)
        plus the injected ``category`` key.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        out: list[dict[str, Any]] = []
        for category, directions in status_map.items():
            rows = self.get_drilldown_data(
                {category: list(directions)},
                node_id=node,
                dwell_time=dwell_time,
                shipment_types=shipment_types,
                size=size,
            )
            for r in rows:
                row = dict(r)
                row["category"] = category
                out.append(row)
        return out

    def fetch_onroad_rows(
        self,
        *,
        node_id: str | None = None,
        status_map: dict[str, list[str]] | None = None,
        dwell_time: Iterable[str] | None = None,
    ) -> list[dict[str, str]]:
        """Return normalised On Road rows for one status map.

        Pulls the ageing drill-down and normalises each package into the
        columns the consuming tools expect (``Tracking ID`` / ``State`` /
        ``Reason`` / ``DSP Name`` / ``Route Code`` / ``Station`` + optional
        fields). The drill-down decorates / enum-codes ``state``; we strip
        the direction suffix and map the enum to the human label the tools
        key off (``"Delivery Failed"`` / ``"In Transit"`` / ...).
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        rows = self.get_drilldown_data(
            status_map or ONROAD_STATUS_MAP,
            node_id=node,
            dwell_time=dwell_time,
            size=10000,
        )
        out: list[dict[str, str]] = []
        for r in rows:
            tid = str(r.get("trackingId") or "").strip()
            if not tid:
                continue
            state_raw = str(r.get("state") or "").strip()
            # Drop any " (direction)" suffix, then map the enum to the human
            # label the First Attempt CSV/tool expects.
            state_clean = re.sub(r"\s*\(.*\)\s*$", "", state_raw).strip() or state_raw
            state = _ONROAD_STATE_LABELS.get(
                state_clean.upper().replace(" ", "_"), state_clean
            )
            out.append(
                {
                    "trackingId": tid,
                    "state": state,
                    "reason": str(r.get("reason") or "").strip(),
                    "dspName": str(r.get("dspName") or "").strip(),
                    "routeCode": str(r.get("routeCode") or "").strip(),
                    "stationCode": str(r.get("stationCode") or node).strip(),
                    "sortZone": str(r.get("sortZone") or r.get("assignedSortZone") or "").strip(),
                    "aisle": str(r.get("aisle") or "").strip(),
                    # Optional fields consumed by other tools (Flow Meeting
                    # uses lastScanBy as the transporter id; the rest enrich
                    # its driver/route activity table).
                    "lastScanBy": str(r.get("lastScanBy") or "").strip(),
                    # Preserve the package timestamp so Cierre can enforce
                    # that Pendientes/Self Debrief belong to the selected day.
                    "lastUpdatedTime": str(r.get("lastUpdatedTime") or "").strip(),
                    "dispatchTime": str(r.get("dispatchTime") or "").strip(),
                    "minutesInState": str(r.get("minutesInState") or "").strip(),
                    # SCC sends the amount in cents (462000 -> 4620.00 EUR).
                    "orderAmount": _amount_to_eur(r.get("orderAmount")),
                    "promisedDeliveryDate": str(
                        r.get("promisedDeliveryDate") or ""
                    ).strip(),
                }
            )
        return out

    def fetch_onroad_combined(
        self,
        *,
        node_id: str | None = None,
        variant: str = "both",
    ) -> list[dict[str, str]]:
        """Return the On Road rows for a ``variant``, deduped by tracking ID.

        * ``failed``  — the Ageing failed/departed set (In Transit DS->Customer
          + Delivery Failed / Rejected).
        * ``iniciar`` — the "Iniciar / In Transit" section (Received / Inducted
          / Marked for Reprocess within the 0-1 day dwell window).
        * ``both``    — union of the two (default), so a single SCC extraction
          yields the complete On Road picture.
        """
        v = (variant or "both").strip().lower()
        out: list[dict[str, str]] = []
        seen: set[str] = set()

        def _add(rows: list[dict[str, str]]) -> None:
            for r in rows:
                key = str(r.get("trackingId") or "").strip().upper()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(r)

        if v in ("failed", "both"):
            # Two queries, failed FIRST: one drill-down for the whole map is
            # capped at 10 000 rows and a big station (DCZ4: >10 000 In
            # Transit at mid-morning) filled it with In Transit only — every
            # Delivery Failed row was silently cut off (verified 2026-10-20:
            # 10 000 In Transit / 0 failed vs 174 failed when asked alone).
            _add(self.fetch_onroad_rows(node_id=node_id, status_map=ONROAD_FAILED_STATUS_MAP))
            _add(self.fetch_onroad_rows(node_id=node_id, status_map=ONROAD_TRANSIT_STATUS_MAP))
        if v in ("iniciar", "in_transit", "intransit", "both"):
            _add(
                self.fetch_onroad_rows(
                    node_id=node_id,
                    status_map=INICIAR_STATUS_MAP,
                    dwell_time=INICIAR_DWELL,
                )
            )
        return out

    def fetch_search_rows(
        self,
        tracking_ids: Iterable[str],
        *,
        node_id: str | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, str]]:
        """SCC "Search" bulk lookup: paste tracking IDs -> package details.

        Reproduces the ``/station/dashboard/search`` textarea flow via
        ``batchGetPackageSummary``. Returns one normalised row per package in
        the same column shape as the On Road rows so the result can be written
        as a CSV the tools already parse. ``State`` keeps the real
        ``currentPackageState`` (e.g. ``Received`` = returned) — we do NOT
        apply the On Road label map here, since "Received" means a return in
        this context.
        """
        summaries = self.batch_get_package_summary(
            tracking_ids, node_id=node_id, progress=progress
        )
        node = (node_id or self.node_id or "").strip().upper()
        out: list[dict[str, str]] = []
        for s in summaries:
            tid = str(s.get("trackingId") or s.get("packageId") or "").strip()
            if not tid:
                continue
            state_raw = str(s.get("currentPackageState") or "").strip()
            state = _SEARCH_STATE_LABELS.get(
                state_raw.upper().replace(" ", "_"),
                " ".join(w.capitalize() for w in state_raw.replace("_", " ").split()),
            )
            provider = ""
            add_attrs = s.get("additionalAttributes")
            if isinstance(add_attrs, dict):
                prov = add_attrs.get("PROVIDER_ID")
                if isinstance(prov, dict):
                    provider = str(prov.get("value") or "").strip()
            out.append(
                {
                    "trackingId": tid,
                    "state": state,
                    "reason": str(s.get("reasonCode") or "").strip(),
                    "dspName": provider or str(s.get("providerId") or "").strip(),
                    "routeCode": str(s.get("currentRouteCode") or "").strip(),
                    "stationCode": str(s.get("currentStationCode") or node).strip(),
                    "sortZone": str(s.get("assignedSortZone") or s.get("sortZone") or "").strip(),
                    "aisle": str(s.get("aisle") or "").strip(),
                    # Assigned DA (Transporter / Driver ID). Exposed as its own
                    # field so consumers (e.g. the weekly DNR report) can render
                    # a dedicated "Driver ID" column instead of digging it out
                    # of "Last Scan By".
                    "driverId": str(s.get("driverId") or "").strip(),
                    # ``driverId`` is the assigned DA; fall back to the last
                    # associate who processed the package.
                    "lastScanBy": str(
                        s.get("driverId") or s.get("lastProcessedByAssociateName") or ""
                    ).strip(),
                    "dispatchTime": "",
                    "minutesInState": str(s.get("minutesInState") or "").strip(),
                    # SCC sends the amount in cents (462000 -> 4620.00 EUR).
                    "orderAmount": _amount_to_eur(s.get("orderAmount")),
                    "promisedDeliveryDate": str(s.get("promisedDeliveryDate") or "").strip(),
                    # Delivery location. SCC redacts the street line (PII) but
                    # returns city / postal / province, which is what the SCC
                    # package search shows as the address.
                    "city": str(s.get("city") or "").strip(),
                    "postal": str(s.get("postal") or "").strip(),
                    "province": str(s.get("province") or "").strip(),
                }
            )
        return out

    # -- receive dashboard line hauls -------------------------------------

    def get_node_linehaul_list(
        self,
        *,
        node_id: str | None = None,
        group_by: str = "",
    ) -> list[dict[str, Any]]:
        """Return the raw ``lineHauls`` from the SCC "receive" dashboard.

        Reproduces the receive board's ``/ivs/getNodeLineHaulList`` call: the
        inbound line hauls for the node with their status, target dock door,
        planned + actual times and volume. Rows keep the SCC field names
        (``lineHaulId`` / ``lineHaulStatus`` / ``lineHaulTime`` / ``origin`` /
        ``targetDockDoor`` / ``volume`` / ``unloadingSequence``).
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        data = self.proxy_call(
            "/ivs/getNodeLineHaulList",
            "induct",
            http_method="post",
            request_body={"nodeId": node, "groupBy": group_by},
        )
        if isinstance(data, dict):
            return data.get("lineHauls") or []
        return []

    def fetch_linehaul_rows(
        self,
        *,
        node_id: str | None = None,
    ) -> list[dict[str, str]]:
        """Return normalised inbound-truck rows for the receive board.

        Each row matches the shape the Flow Meeting NS Camiones tables consume
        (``vrid`` / ``status`` / ``origin`` / ``dock`` / ``planned`` /
        ``actual`` / ``volume`` / ``unloadSeq``) — i.e. exactly what the
        operator used to copy/paste from the receive box. Times are rendered
        ``HH:MM`` (local wall-clock, as SCC emits them) and the
        ``lineHaulStatus`` enum is mapped to a label the tool classifies on.
        """
        rows = self.get_node_linehaul_list(node_id=node_id)
        out: list[dict[str, str]] = []
        for r in rows:
            vrid = str(r.get("lineHaulId") or "").strip()
            if not vrid:
                continue
            status_raw = str(r.get("lineHaulStatus") or "").strip().upper()
            status = _LINEHAUL_STATUS_LABELS.get(
                status_raw,
                " ".join(w.capitalize() for w in status_raw.replace("_", " ").split()),
            )
            times = r.get("lineHaulTime") or {}
            if not isinstance(times, dict):
                times = {}
            planned = _linehaul_hhmm(times.get("plannedTime"))
            actual = _linehaul_hhmm(times.get("actualTime")) or _linehaul_hhmm(
                times.get("departedTime")
            )
            vol = r.get("volume")
            try:
                volf = float(vol)
                volume = str(int(round(volf))) if volf > 0 else ""
            except (TypeError, ValueError):
                volume = ""
            seq = r.get("unloadingSequence")
            try:
                seq_i = int(round(float(seq)))
                unload_seq = str(seq_i) if seq_i >= 0 else ""
            except (TypeError, ValueError):
                unload_seq = ""
            out.append(
                {
                    "vrid": vrid,
                    "status": status,
                    "origin": str(r.get("origin") or "").strip(),
                    "dock": str(r.get("targetDockDoor") or "").strip(),
                    "planned": planned,
                    "actual": actual,
                    "volume": volume,
                    "unloadSeq": unload_seq,
                }
            )
        return out

    def fetch_arrived_linehaul_volume(
        self,
        *,
        node_id: str | None = None,
    ) -> dict[str, int]:
        """Sum the receive-board volume for line hauls that have arrived.

        SCC returns the language-neutral ``lineHaulStatus`` enum even when
        the dashboard labels are localized.  Filtering on the raw
        ``ARRIVED`` value therefore covers "Llegó", "Arrived" and every
        other translated display label without maintaining a text allowlist.
        """
        rows = self.get_node_linehaul_list(node_id=node_id)
        total = 0.0
        arrived_count = 0
        for row in rows:
            status = str(row.get("lineHaulStatus") or "").strip().upper()
            if status != "ARRIVED":
                continue
            arrived_count += 1
            try:
                volume = float(row.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(volume):
                total += volume
        return {
            "total": int(round(total)),
            "arrived_count": arrived_count,
            "row_count": len(rows),
        }

    # -- flow PVA (induct / stow hourly throughput) -----------------------

    def get_flow_pva(
        self,
        *,
        node_id: str | None = None,
        cycle_ids: Iterable[str] = ("CYCLE_1",),
        process_path: str = "induct",
    ) -> dict[str, Any]:
        """Return the raw ``flowPVAData`` for a process path.

        Reproduces the SCC induct dashboard's ``/ivs/getpvadata`` call. The
        ``induct`` process path carries BOTH the induct (``IND``) and stow
        (``SORTED``) series, each split into an ``Actual`` / ``Planned``
        ``taskCountType`` and bucketed at 15 / 30 / 60-minute intervals
        (``flowPVAData`` is keyed by the interval, e.g. ``"15"``).
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        data = self.proxy_call(
            "/ivs/getpvadata",
            "induct",
            http_method="post",
            request_body={
                "nodeId": node,
                "cycleIds": list(cycle_ids),
                "processPath": process_path,
            },
        )
        if isinstance(data, dict):
            return data.get("flowPVAData") or {}
        return {}

    def fetch_induct_stow_series(
        self,
        *,
        node_id: str | None = None,
        cycle_ids: Iterable[str] = ("CYCLE_1",),
        interval: str = "15",
    ) -> dict[str, list[tuple[int, float]]]:
        """Return induct/stow *actual* throughput as ``(epoch_s, value)`` lists.

        Keys ``induct`` and ``stow`` map to the ``IND`` / ``SORTED`` metric
        names — the stable enum keys the gateway returns, NOT the localized
        display labels — so this keeps working on a station whose dashboard
        renders in another language. Only the ``Actual`` task-count type is
        returned; negative sentinels (``-1`` = "no data / no plan") are
        dropped. Falls back to the 60-minute buckets if the requested
        interval is absent.
        """
        fpva = self.get_flow_pva(node_id=node_id, cycle_ids=cycle_ids)
        return {
            "induct": _pva_points(fpva, "IND", interval),
            "stow": _pva_points(fpva, "SORTED", interval),
        }

    # -- at-station dwell summary (total + aisle breakdown) ---------------

    def fetch_at_station_summary(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
        lookback_hours: int = DWELL_DEFAULT_LOOKBACK_HOURS,
        over_minutes: int = 90,
    ) -> dict[str, Any]:
        """Return the SCC "At Station" dwell total + aisle breakdown.

        Reproduces the dwell dashboard drill-down (``Inducted`` /
        ``Stow Problem Solve`` / ``Stow Buffered`` within the cycle) and
        aggregates it into the numbers the Flow Meeting NS "At Station" row
        needs: the total dwelling count, the per-aisle counts (highest first)
        and the count dwelling ``>= over_minutes`` (the "90' parcels"
        figure, derived from the same rows). De-dupes by ``trackingId``.
        """
        rows = self.get_dwell_drilldown(
            DWELL_ERRORS_STATUS_MAP,
            node_id=node_id,
            cycles=list(cycles),
            lookback_hours=lookback_hours,
        )
        return _aggregate_dwell_rows(rows, over_minutes=over_minutes)

    def fetch_sideline_summary(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
        lookback_hours: int = DWELL_DEFAULT_LOOKBACK_HOURS,
    ) -> dict[str, Any]:
        """Return the SCC "Sideline" (Held) cumulative total + aisle breakdown.

        Reproduces the SCC "Held" drill-down for the cycle (the tile the
        operator reads as the accumulated sideline) and returns the total
        package count plus the per-aisle counts (highest first).
        """
        rows = self.get_dwell_drilldown(
            {"Held": []},
            node_id=node_id,
            cycles=list(cycles),
            lookback_hours=lookback_hours,
        )
        return _aggregate_dwell_rows(rows)

    def fetch_retained_summary(self, *, node_id: str | None = None) -> dict[str, Any]:
        """Return the ageing dashboard's "Retained / Retenido" row total.

        Mirrors the number the operator reads on
        ``/station/dashboard/ageing`` — the TOTAL cell of the Retained row —
        by scanning the ageing summary (``getDwellingPackageData``,
        ``AGEING_VIEW``) for the row whose ``packageStatus`` matches
        Retained / Retenido (the API label is stable across UI languages,
        but we match loosely to be safe). When the summary doesn't expose
        the row, falls back to counting the retained drill-down categories
        (:data:`RETAINED_STATUS_MAP`) de-duplicated by tracking ID.
        """
        rows = self.get_dwelling_package_data(node_id=node_id, view="AGEING_VIEW")
        for r in rows:
            status = str(r.get("packageStatus") or "").strip().lower()
            if "retain" in status or "retenid" in status:
                cell = (r.get("columnToViewDataMap") or {}).get("TOTAL") or {}
                try:
                    total = int(float(cell.get("value") or 0))
                except (TypeError, ValueError):
                    total = 0
                return {"total": total, "source": "ageing_summary"}
        # Fallback: sum the retained categories via drill-down (no dwell
        # filter, so the count covers every dwell column like the TOTAL cell).
        drill = self.fetch_categorized_drilldown(RETAINED_STATUS_MAP, node_id=node_id)
        seen: set[str] = set()
        total = 0
        for r in drill:
            tid = str(r.get("trackingId") or "").strip().upper()
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            total += 1
        return {"total": total, "source": "retained_drilldown"}

    # -- FSAF (associate stow verification) -------------------------------

    def fetch_associates(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
    ) -> list[dict[str, Any]]:
        """Return the raw ``associates`` list from the SCC Associate Sort view.

        Reproduces the ``svs/associates/data`` call the Associate Sort
        dashboard makes. Each associate carries a ``status`` (``ACTIVE`` /
        ``INACTIVE``), a ``location`` (their última ubicación / aisle) and a
        ``performance`` block that includes the ``fsaf`` count.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        data = self.proxy_call(
            "svs/associates/data",
            "stow",
            http_method="post",
            request_body={
                "filters": {"NODE": [node], "CYCLE": list(cycles)},
                "fieldsRequired": ["NAME", "STATUS", "PERFORMANCE", "LOCATION"],
            },
        )
        if isinstance(data, dict):
            return data.get("associates") or []
        return []

    def fetch_pick_associates(
        self,
        *,
        node_id: str | None = None,
        date: str | None = None,
    ) -> list[str]:
        """Return the distinct pick-associate logins from the SCC
        "Associate Pick" dashboard (its "Associate" column).

        Reproduces the ``/apigw/route/station/<node>`` (``routeStaging``)
        call the ``/station/dashboard/associatepick`` page makes: each
        route carries ``pickAssociates`` (and ``picklists[].associates``)
        entries whose ``alias`` / ``emailId`` identify the AA. Returns the
        deduplicated logins ("juanbarx@amazon.com" -> "juanbarx"), sorted
        alphabetically.

        ``date`` uses the dashboard's own ``M/D/YYYY`` format (no zero
        padding); defaults to today.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        if not date:
            now = time.localtime()
            date = f"{now.tm_mon}/{now.tm_mday}/{now.tm_year}"
        data = self.proxy_call(
            f"/apigw/route/station/{node}",
            "routeStaging",
            http_method="get",
            request_params={"date": [date]},
        )

        logins: set[str] = set()

        def _collect(entries: Any) -> None:
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                alias = str(
                    entry.get("alias") or entry.get("emailId") or entry.get("name") or ""
                ).strip()
                login = alias.split("@", 1)[0].strip().lower()
                if login:
                    logins.add(login)

        routes = data.get("routes") if isinstance(data, dict) else None
        for route in routes or []:
            if not isinstance(route, dict):
                continue
            _collect(route.get("pickAssociates"))
            for picklist in route.get("picklists") or []:
                if isinstance(picklist, dict):
                    _collect(picklist.get("associates"))
        return sorted(logins)

    def fetch_fsaf_summary(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
        active_only: bool = True,
    ) -> dict[str, Any]:
        """Return the FSAF total + top aisles from the Associate Sort view.

        Sums each associate's ``performance.fsaf`` — by default only for
        ``ACTIVE`` associates (the operator's filter) — and groups the FSAF
        count by ``location`` (última ubicación) for the top-aisle hover.
        """
        associates = self.fetch_associates(node_id=node_id, cycles=cycles)
        total = 0.0
        loc_fsaf: dict[str, float] = {}
        for a in associates:
            status = str(a.get("status") or "").strip().upper()
            if active_only and status != "ACTIVE":
                continue
            perf = a.get("performance") or {}
            try:
                fsaf = float(perf.get("fsaf") or 0)
            except (TypeError, ValueError):
                fsaf = 0.0
            if fsaf < 0:
                fsaf = 0.0
            total += fsaf
            location = str(a.get("location") or "").strip() or "—"
            loc_fsaf[location] = loc_fsaf.get(location, 0.0) + fsaf
        aisles = sorted(
            ((k, v) for k, v in loc_fsaf.items() if v > 0),
            key=lambda kv: (-kv[1], kv[0]),
        )
        return {
            "total": int(round(total)),
            "active_only": active_only,
            "aisles": [{"aisle": a, "fsaf": int(round(v))} for a, v in aisles],
        }

    # -- induction lines / fingers live snapshot ---------------------------

    def _resolve_api_cycle_id(
        self, node: str, cycle_name: str = "CYCLE_1"
    ) -> str | None:
        """Best-effort resolution of the plan's dynamic ``cycleId`` (UUID).

        The stow metrics call (``svs/packages/metrics``) mirrors the SCC
        stow dashboard, which sends BOTH the static cycle name (``CYCLE_1``)
        and the plan's per-day ``cycleId`` UUID. We resolve it the same way
        the dashboard does: ``flow/target/data`` names the current plan
        (``planIdentifier: "SATURDAY/CYCLE_1"``) and ``station-plan/data``
        carries each plan's details. Any failure resolves to ``None`` — the
        metrics call still answers without the UUID filter, so this must
        never raise. Cached per instance (the plan doesn't change within a
        client's lifetime).
        """
        cache: dict[str, str | None] = getattr(self, "_api_cycle_ids", None) or {}
        if not hasattr(self, "_api_cycle_ids"):
            self._api_cycle_ids = cache
        cache_key = f"{node}/{cycle_name}"
        if cache_key in cache:
            return cache[cache_key]

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        resolved: str | None = None
        try:
            target = self._session.get(
                f"{SCC_BASE_URL}/station/flow/target/data"
                f"?stationCode={node}&cycleId={cycle_name}",
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
            )
            plan_identifier = ""
            if target.status_code == 200:
                plan_identifier = str((target.json() or {}).get("planIdentifier") or "")
            if "/" in plan_identifier:
                day, plan_cycle = plan_identifier.split("/", 1)
                plans = self._session.get(
                    f"{SCC_BASE_URL}/station/station-plan/data?stationCode={node}",
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if plans.status_code == 200:
                    entries = (plans.json() or {}).get("stationPlanWithTargetsList") or []
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        details = entry.get("planDetails") or {}
                        if (
                            entry.get("dayOfTheWeek") == day
                            and details.get("cycleName") == plan_cycle
                        ):
                            resolved = str(details.get("cycleId") or "") or None
                            break
        except (requests.RequestException, ValueError):
            resolved = None

        cache[cache_key] = resolved
        return resolved

    def fetch_induction_lines(self, *, node_id: str | None = None) -> dict[str, Any]:
        """Return the live induction-line metrics (SCC induct dashboard).

        Reproduces the ``/ivs/getLocationMetric`` call: one row per induct
        line with the volume inducted, hourly rate and operator count. Idle
        lines (no operators AND no volume) are dropped — they carry no
        signal. Active lines sort first, highest volume on top.

        The gateway nests ``locationMetrics`` at different depths depending
        on the deployment (top-level, under ``body``, deeper), so the list
        is located recursively (same tolerant lookup the dashboard's own
        JS performs) — a rigid two-level check silently returned 0 lines
        on stations where the envelope differs.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        data = self.proxy_call(
            "/ivs/getLocationMetric",
            "induct",
            http_method="post",
            request_body={"nodeId": node},
        )
        raw = _find_location_metrics(data)

        items: list[dict[str, Any]] = []
        total_inducted = 0
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            try:
                inducted = int(float(row.get("volumeInducted") or 0))
                rate = int(float(row.get("inductRate") or 0))
                operators = int(float(row.get("operatorCount") or 0))
            except (TypeError, ValueError):
                continue
            if operators <= 0 and inducted <= 0:
                continue
            total_inducted += inducted
            items.append(
                {
                    # Blank stays blank: the widget renders the locale-neutral
                    # "—" placeholder (a backend-composed "N/A" would leak an
                    # unlocalized literal into the line cards).
                    "location": str(row.get("locationName") or "").strip(),
                    "inducted": inducted,
                    "rate": rate,
                    "per_minute": int(round(rate / 60)),
                    "operators": operators,
                    "active": operators > 0,
                }
            )
        items.sort(key=lambda it: (not it["active"], -it["inducted"], it["location"]))
        return {"items": items, "total_inducted": total_inducted}

    def fetch_finger_metrics(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
    ) -> dict[str, Any]:
        """Return the per-finger (CLUSTER) stow metrics + station totals.

        Reproduces the ``svs/packages/metrics`` call the SCC stow dashboard
        makes grouped by CLUSTER: current inducted / stow-buffered counts and
        the stow rate per finger, plus the aggregated station totals. The
        plan's dynamic ``cycleId`` UUID rides along when resolvable (see
        :meth:`_resolve_api_cycle_id`); clusters with no plan and no volume
        are dropped.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        cycle_list = [str(c) for c in cycles if str(c)] or ["CYCLE_1"]
        filters: dict[str, list[str]] = {
            "NODE": [node],
            "DRS": ["FALSE"],
            "CYCLE": cycle_list,
        }
        api_cycle_id = self._resolve_api_cycle_id(node, cycle_list[0])
        if api_cycle_id:
            filters["CYCLE_ID"] = [api_cycle_id]

        data = self.proxy_call(
            "svs/packages/metrics",
            "stow",
            http_method="post",
            request_body={
                "groupBy": "CLUSTER",
                "filters": filters,
                "metrics": [
                    "STOW_RATE",
                    "CURRENT_INDUCTED_COUNT",
                    "CURRENT_STOW_BUFFERED_COUNT",
                    "PLANNED_COUNT",
                ],
                "isAggregationRequired": True,
            },
        )
        body: Any = data
        if (
            isinstance(data, dict)
            and "groupedPackageMetrics" not in data
            and isinstance(data.get("body"), dict)
        ):
            body = data["body"]
        groups = (body or {}).get("groupedPackageMetrics") or {}
        totals_raw = (body or {}).get("aggregatePackageMetrics") or {}

        def _num(value: Any) -> int:
            try:
                return int(round(float(value or 0)))
            except (TypeError, ValueError):
                return 0

        clusters: list[dict[str, Any]] = []
        for key, metrics in groups.items():
            if not isinstance(metrics, dict):
                continue
            planned = _num(metrics.get("plannedCount"))
            inducted = _num(metrics.get("currentInductedCount"))
            buffered = _num(metrics.get("currentStowBufferedCount"))
            if planned <= 0 and inducted <= 0 and buffered <= 0:
                continue
            clusters.append(
                {
                    "cluster": str(key),
                    "inducted": inducted,
                    "buffered": buffered,
                    "planned": planned,
                    "rate": _num(metrics.get("stowRate")),
                }
            )
        clusters.sort(key=lambda c: c["cluster"])
        return {
            "clusters": clusters,
            "totals": {
                "inducted": _num(totals_raw.get("currentInductedCount")),
                "buffered": _num(totals_raw.get("currentStowBufferedCount")),
                "rate": _num(totals_raw.get("stowRate")),
            },
        }

    def fetch_open_aisles(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
    ) -> dict[str, Any]:
        """Return the per-aisle stow metrics — i.e. which aisles are OPEN.

        Reproduces the ``svs/packages/metrics`` call grouped by ``AISLE``
        (the same query the SCC stow dashboard — and the operator's Flow
        Layout tampermonkey — makes): every aisle that carries a plan or
        live volume for the cycle shows up in ``groupedPackageMetrics``.
        An aisle counts as *open* when it has planned OR live volume;
        aisles with every metric at 0 are dropped (closed / no signal).

        Returns ``{"aisles": [{"aisle", "planned", "inducted",
        "buffered"}], "labels": [...]}`` with labels uppercased so they
        match the STEM layout labels (``A1``, ``C38``, ...).
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        cycle_list = [str(c) for c in cycles if str(c)] or ["CYCLE_1"]
        filters: dict[str, list[str]] = {
            "NODE": [node],
            "DRS": ["FALSE"],
            "CYCLE": cycle_list,
        }
        api_cycle_id = self._resolve_api_cycle_id(node, cycle_list[0])
        if api_cycle_id:
            filters["CYCLE_ID"] = [api_cycle_id]

        data = self.proxy_call(
            "svs/packages/metrics",
            "stow",
            http_method="post",
            request_body={
                "groupBy": "AISLE",
                "filters": filters,
                "metrics": [
                    "PLANNED_COUNT",
                    "CURRENT_INDUCTED_COUNT",
                    "CURRENT_STOW_BUFFERED_COUNT",
                ],
            },
        )
        body: Any = data
        if (
            isinstance(data, dict)
            and "groupedPackageMetrics" not in data
            and isinstance(data.get("body"), dict)
        ):
            body = data["body"]
        groups = (body or {}).get("groupedPackageMetrics") or {}

        def _num(value: Any) -> int:
            try:
                return int(round(float(value or 0)))
            except (TypeError, ValueError):
                return 0

        aisles: list[dict[str, Any]] = []
        for key, metrics in groups.items():
            if not isinstance(metrics, dict):
                continue
            label = str(key or "").strip().upper()
            if not label or label == "TOTAL":
                continue
            planned = _num(metrics.get("plannedCount"))
            inducted = _num(metrics.get("currentInductedCount"))
            buffered = _num(metrics.get("currentStowBufferedCount"))
            if planned <= 0 and inducted <= 0 and buffered <= 0:
                continue
            aisles.append(
                {
                    "aisle": label,
                    "planned": planned,
                    "inducted": inducted,
                    "buffered": buffered,
                }
            )
        aisles.sort(key=lambda a: a["aisle"])
        return {"aisles": aisles, "labels": [a["aisle"] for a in aisles]}

    def fetch_bins_missing_bags(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
    ) -> dict[str, Any]:
        """Return the aisles with bins missing an open bag ("Falta").

        Reproduces the SCC stow dashboard's "Falta" column
        (``svs/bins/metrics`` with ``BINS_COUNT_MISSING_OPEN_BAG``): one
        CLUSTER-grouped call for the per-finger totals, then one AISLE
        drill-down per cluster that reports missing bins — exactly the
        sequence the dashboard fires when the operator opens a cluster.
        Unlike the packages metrics, the dashboard sends NO ``DRS``
        filter here (captured from the live HAR), so neither do we.

        Returns ``{"clusters": [{"cluster", "active_bins", "missing"}],
        "aisles": [{"aisle", "cluster", "active_bins", "missing"}],
        "total_missing": N}`` with labels uppercased so they match the
        STEM layout labels (``A1``, ``C38``, ...).
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        cycle_list = [str(c) for c in cycles if str(c)] or ["CYCLE_1"]
        filters: dict[str, list[str]] = {"NODE": [node], "CYCLE": cycle_list}
        api_cycle_id = self._resolve_api_cycle_id(node, cycle_list[0])
        if api_cycle_id:
            filters["CYCLE_ID"] = [api_cycle_id]

        def _num(value: Any) -> int:
            try:
                return int(round(float(value or 0)))
            except (TypeError, ValueError):
                return 0

        def _bin_groups(request_body: dict[str, Any]) -> dict[str, Any]:
            data = self.proxy_call(
                "svs/bins/metrics",
                "stow",
                http_method="post",
                request_body=request_body,
            )
            body: Any = data
            if (
                isinstance(data, dict)
                and "groupedBinMetrics" not in data
                and isinstance(data.get("body"), dict)
            ):
                body = data["body"]
            return (body or {}).get("groupedBinMetrics") or {}

        clusters: list[dict[str, Any]] = []
        for key, metrics in _bin_groups(
            {
                "groupBy": "CLUSTER",
                "filters": filters,
                "metrics": ["BINS_COUNT_MISSING_OPEN_BAG"],
            }
        ).items():
            if not isinstance(metrics, dict):
                continue
            label = str(key or "").strip().upper()
            active = _num(metrics.get("activeBinsCount"))
            missing = _num(metrics.get("binsCountMissingOpenBag"))
            # Placeholder clusters (AA_A, CB_A, ...) come back with 0
            # active bins and 0 missing: no signal, drop them.
            if not label or (active <= 0 and missing <= 0):
                continue
            clusters.append(
                {"cluster": label, "active_bins": active, "missing": missing}
            )
        clusters.sort(key=lambda c: c["cluster"])

        aisles: list[dict[str, Any]] = []
        for cluster in clusters:
            if cluster["missing"] <= 0:
                continue
            drill_filters = dict(filters)
            drill_filters["CLUSTER"] = [cluster["cluster"]]
            for key, metrics in _bin_groups(
                {
                    "groupBy": "AISLE",
                    "filters": drill_filters,
                    "metrics": ["BINS_COUNT_MISSING_OPEN_BAG"],
                }
            ).items():
                if not isinstance(metrics, dict):
                    continue
                label = str(key or "").strip().upper()
                missing = _num(metrics.get("binsCountMissingOpenBag"))
                if not label or label == "TOTAL" or missing <= 0:
                    continue
                aisles.append(
                    {
                        "aisle": label,
                        "cluster": cluster["cluster"],
                        "active_bins": _num(metrics.get("activeBinsCount")),
                        "missing": missing,
                    }
                )
        aisles.sort(key=lambda a: a["aisle"])
        return {
            "clusters": clusters,
            "aisles": aisles,
            "total_missing": sum(c["missing"] for c in clusters),
        }

    def fetch_flow_totals(
        self,
        *,
        node_id: str | None = None,
        cycle: str = "CYCLE_1",
    ) -> dict[str, Any]:
        """Return the SCC flow headline totals: INDUCT, STOW and SIDELINE.

        Mirrors the numbers the station dashboard header shows: the induct /
        stow counters come from the ``/station/flow/{induct,sort}/data`` GETs
        and the current sideline from ``/ivs/getPackageMetric``
        (``SIDELINE``). Best-effort per metric — a metric that cannot be
        read comes back as ``None`` instead of failing the whole snapshot
        (auth problems still surface via :meth:`_ensure_key`).
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        key = self._ensure_key()
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, */*",
            "X-API-Usage-Key": key,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self._page_url,
        }
        out: dict[str, Any] = {"induct": None, "stow": None, "sideline": None}
        for name, path in (
            ("induct", "/station/flow/induct/data"),
            ("stow", "/station/flow/sort/data"),
        ):
            try:
                resp = self._session.get(
                    f"{SCC_BASE_URL}{path}?stationCode={node}&cycleId={cycle}",
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if resp.status_code == 200:
                    out[name] = int(float((resp.json() or {}).get("value") or 0))
            except (requests.RequestException, ValueError, TypeError):
                continue
        try:
            data = self.proxy_call(
                "/ivs/getPackageMetric",
                "induct",
                http_method="post",
                request_body={
                    "nodeId": node,
                    "groupBy": "Node",
                    "filters": {"Cycle": []},
                    "metricList": ["SIDELINE"],
                },
            )
            grouped = (data or {}).get("groupedPackageMetrics") or {}
            sideline_map = grouped.get("SIDELINE") or {}
            out["sideline"] = int(float(sideline_map.get(node) or 0))
        except (SccAuthError, SccClientError, ValueError, TypeError):
            pass
        return out

    def fetch_stow_wip(
        self,
        *,
        node_id: str | None = None,
        cycle: str = "CYCLE_1",
    ) -> dict[str, Any]:
        """Return the STOW-WIP headline: value, compliance, targets, trend.

        Reproduces the stow dashboard's ``/svs/getMetricData`` batch: the
        instantaneous ``STOW_WIP`` value, the ``STOW_WIP_COMPLIANCE_DATA``
        percentage and the ``STOW_WIP_INTERVAL_DATA`` series (which carries
        the LOWER/UPPER targets plus the 15-minute trend points).
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        # The metric endpoint expects both the static cycle name and the
        # current plan's dynamic cycle UUID. Sending ``CYCLE_1`` as
        # ``CYCLE_ID`` makes SCC return HTTP 500, while the same dimensions
        # used by the stow dashboard return the three WIP metrics correctly.
        dims = [
            {"name": "STATION_CODE", "value": node},
            {"name": "CYCLE", "value": cycle},
        ]
        api_cycle_id = self._resolve_api_cycle_id(node, cycle)
        if api_cycle_id:
            dims.append({"name": "CYCLE_ID", "value": api_cycle_id})

        data = self.proxy_call(
            "/svs/getMetricData",
            "stow",
            http_method="post",
            request_body={
                "metricDataQueries": [
                    {"metric": {"metricName": "STOW_WIP", "dimensions": dims}},
                    {"metric": {"metricName": "STOW_WIP_COMPLIANCE_DATA", "dimensions": dims}},
                    {
                        "metric": {
                            "metricName": "STOW_WIP_INTERVAL_DATA",
                            "dimensions": dims,
                            "period": [15],
                            "statistic": "SUM",
                        }
                    },
                ]
            },
        )

        def _fnum(value: Any) -> float | None:
            try:
                return round(float(value), 1)
            except (TypeError, ValueError):
                return None

        out: dict[str, Any] = {
            "value": None,
            "compliance": None,
            "lower": 0,
            "upper": 0,
            "trend": [],
        }
        # Gateway deployments may return the downstream payload directly or
        # under ``body``. Accept both, but do not turn an unknown/empty
        # envelope into a successful zero reading.
        body: Any = data
        if (
            isinstance(data, dict)
            and "metricResults" not in data
            and isinstance(data.get("body"), dict)
        ):
            body = data["body"]
        results = (body or {}).get("metricResults") if isinstance(body, dict) else None
        if not isinstance(results, list) or not results:
            raise SccClientError("SCC no devolvio las metricas de Stow WIP.")

        recognized = False
        for res in results:
            if not isinstance(res, dict):
                continue
            name = str((res.get("metric") or {}).get("metricName") or "")
            if name in {
                "STOW_WIP",
                "STOW_WIP_COMPLIANCE_DATA",
                "STOW_WIP_INTERVAL_DATA",
            }:
                recognized = True
            values = res.get("metricValues") or []
            if name == "STOW_WIP":
                for mv in values:
                    if isinstance(mv, dict) and mv.get("name") == "STOW_WIP":
                        out["value"] = _fnum(mv.get("value"))
            elif name == "STOW_WIP_COMPLIANCE_DATA":
                for mv in values:
                    if isinstance(mv, dict) and mv.get("name") == "STOW_WIP":
                        out["compliance"] = _fnum(mv.get("value"))
            elif name == "STOW_WIP_INTERVAL_DATA":
                for mv in values:
                    if not isinstance(mv, dict):
                        continue
                    if mv.get("name") == "STOW_WIP_LOWER_TARGET":
                        out["lower"] = int(_fnum(mv.get("value")) or 0)
                    elif mv.get("name") == "STOW_WIP_UPPER_TARGET":
                        out["upper"] = int(_fnum(mv.get("value")) or 0)
                    elif "TimeSeriesMetricValue" in str(mv.get("__type") or ""):
                        points = (mv.get("dataPoints") or {}).get("15") or []
                        trend = []
                        for dp in points:
                            if not isinstance(dp, dict):
                                continue
                            val = _fnum(dp.get("value"))
                            try:
                                epoch = int(dp.get("timestamp") or 0)
                                stamp = time.strftime("%H:%M", time.localtime(epoch))
                            except (ValueError, OSError, OverflowError):
                                continue
                            if val is not None:
                                # ``ts`` (epoch seconds) rides along so
                                # consumers can match each 15' point against
                                # absolute windows; ``time`` keeps the HH:MM
                                # the widget renders.
                                trend.append({"time": stamp, "ts": epoch, "value": val})
                        out["trend"] = trend
        if not recognized:
            raise SccClientError("SCC devolvio metricas sin Stow WIP.")
        return out

    def fetch_flow_balance(
        self,
        *,
        node_id: str | None = None,
        cycle_ids: Iterable[str] = ("CYCLE_1",),
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the Induct vs Sorted balance per interval (15' and 60').

        Joins the ``IND`` and ``SORTED`` *Actual* series from the flow PVA
        data by timestamp. ``balance_pct`` = ``(1 - inducted/sorted) * 100``
        (positive = stow keeps up, negative = induct outruns stow); ``None``
        when nothing was sorted in the bucket.
        """
        fpva = self.get_flow_pva(node_id=node_id, cycle_ids=cycle_ids)
        out: dict[str, list[dict[str, Any]]] = {}
        for interval in ("15", "60"):
            inducted: dict[str, float] = {}
            sorted_map: dict[str, float] = {}
            for item in fpva.get(interval) or []:
                if not isinstance(item, dict):
                    continue
                if item.get("taskCountType") != "Actual":
                    continue
                name = item.get("metricName")
                if name not in ("IND", "SORTED"):
                    continue
                for point in item.get("dataPointList") or []:
                    if not isinstance(point, dict):
                        continue
                    stamp = str(point.get("timeStampVal") or "")
                    try:
                        value = float(point.get("metricValue"))
                    except (TypeError, ValueError):
                        continue
                    if value < 0 or not stamp:
                        continue
                    (inducted if name == "IND" else sorted_map)[stamp] = value
            rows: list[dict[str, Any]] = []
            for stamp in sorted(set(inducted) & set(sorted_map)):
                ind = inducted[stamp]
                srt = sorted_map[stamp]
                if ind == 0 and srt == 0:
                    continue
                balance = round((1 - (ind / srt)) * 100, 1) if srt > 0 else None
                rows.append(
                    {
                        "time": stamp.split("T")[1][:5] if "T" in stamp else stamp,
                        "inducted": int(ind),
                        "sorted": int(srt),
                        "balance_pct": balance,
                    }
                )
            out[interval] = rows
        return out

    def fetch_stale_packages(
        self,
        *,
        node_id: str | None = None,
        cycles: Iterable[str] = ("CYCLE_1",),
        threshold_minutes: int = 30,
        limit: int = 60,
    ) -> dict[str, Any]:
        """Return the packages stuck in INDUCTED / SORT_TO_BUFFER state.

        Reproduces the stow dashboard's ``svs/packages/data`` calls for the
        two live statuses and keeps the packages whose ``minutesInState``
        meets the threshold, worst first, plus the >60' / >120' summary
        per finger the operator escalates with.
        """
        node = (node_id or self.node_id or "").strip().upper()
        if not node:
            raise SccClientError("Falta nodeId (station code) para SCC.")
        cycle_list = [str(c) for c in cycles if str(c)] or ["CYCLE_1"]
        items: list[dict[str, Any]] = []
        for status, label in (
            ("CURRENT_INDUCTED", "IND"),
            ("CURRENT_STOW_BUFFERED", "BUFFER"),
        ):
            data = self.proxy_call(
                "svs/packages/data",
                "stow",
                http_method="post",
                request_body={
                    "filters": {
                        "NODE": [node],
                        "CYCLE": cycle_list,
                        "DRS": ["FALSE"],
                        "PACKAGE_STATUS": [status],
                    }
                },
            )
            for pkg in (data or {}).get("packages") or []:
                if not isinstance(pkg, dict):
                    continue
                try:
                    minutes = int(float(pkg.get("minutesInState") or 0))
                except (TypeError, ValueError):
                    minutes = 0
                loc = pkg.get("sortLocation") or {}
                items.append(
                    {
                        "tracking_id": str(pkg.get("trackingId") or ""),
                        "minutes": minutes,
                        "state": label,
                        "cluster": str(loc.get("cluster") or ""),
                        "bin": str(loc.get("bin") or ""),
                        "location": str(loc.get("location") or ""),
                        "size": _SIZE_ABBREVIATIONS.get(
                            str(pkg.get("size") or ""), str(pkg.get("size") or "")
                        ),
                    }
                )
        threshold = max(1, int(threshold_minutes))
        stale = [p for p in items if p["minutes"] >= threshold]
        stale.sort(key=lambda p: -p["minutes"])
        by_cluster: dict[str, dict[str, int]] = {}
        over60 = over120 = 0
        for pkg in items:
            if pkg["minutes"] < 60:
                continue
            entry = by_cluster.setdefault(pkg["cluster"] or "—", {"over60": 0, "over120": 0})
            entry["over60"] += 1
            over60 += 1
            if pkg["minutes"] >= 120:
                entry["over120"] += 1
                over120 += 1
        return {
            "items": stale[: max(1, int(limit))],
            "total": len(stale),
            "scanned": len(items),
            "threshold": threshold,
            "over60": over60,
            "over120": over120,
            "by_cluster": [
                {"cluster": key, **counts}
                for key, counts in sorted(by_cluster.items())
            ],
        }

    def fetch_associate_activity(
        self,
        login: str,
        date_str: str,
        *,
        page_size: int = 100,
        max_pages: int = 40,
    ) -> dict[str, Any]:
        """Return one associate's STOW activity for a local calendar day.

        Pages through ``/os/getPackageHistoryForAssociate`` (oculus) for the
        local midnight→midnight window and summarizes the STOWED events:
        bag vs oversize split (a package stowed INTO a bag scans a container
        different from the location), hourly breakdown and the non-STOWED
        problem states.
        """
        alias = (login or "").strip().lower().split("@", 1)[0]
        if not alias:
            raise SccClientError("Falta el login del asociado.")
        try:
            year, month, day = (int(part) for part in str(date_str).split("-"))
            start_s = time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
        except (TypeError, ValueError, OverflowError) as exc:
            raise SccClientError(f"Fecha inválida: {date_str!r}") from exc
        start_ms = int(start_s * 1000)
        end_ms = start_ms + 24 * 3600 * 1000

        records: list[dict[str, Any]] = []
        token: Any = None
        for _ in range(max(1, int(max_pages))):
            data = self.proxy_call(
                "/os/getPackageHistoryForAssociate",
                "oculus",
                http_method="post",
                request_body={
                    "associateLoginId": f"{alias}@amazon.com",
                    "pageSize": page_size,
                    "pageToken": token,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            )
            if not isinstance(data, dict):
                break
            chunk = (
                data.get("packageRecordList")
                or data.get("packageEventList")
                or data.get("packageHistoryList")
                or []
            )
            records.extend(r for r in chunk if isinstance(r, dict))
            token = data.get("nextPageToken") or None
            if not token:
                break

        def _is_bag(record: dict[str, Any]) -> bool:
            container = str(
                record.get("scanContainer") or record.get("containerId") or ""
            ).strip()
            location = str(
                record.get("scanLocation")
                or record.get("locationId")
                or record.get("sortLocation")
                or ""
            ).strip()
            return bool(container and location and container != location)

        def _event_ms(record: dict[str, Any]) -> int:
            for key in ("eventTime", "scanTime", "lastEventTime"):
                try:
                    value = int(record.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
            return 0

        stowed = [r for r in records if r.get("packageEventState") == "STOWED"]
        bags = sum(1 for r in stowed if _is_bag(r))
        oversize = len(stowed) - bags

        hourly_map: dict[str, dict[str, int]] = {}
        last_ms = 0
        for record in stowed:
            ms = _event_ms(record)
            if ms <= 0:
                continue
            last_ms = max(last_ms, ms)
            hour = time.strftime("%H:00", time.localtime(ms / 1000))
            bucket = hourly_map.setdefault(hour, {"total": 0, "bags": 0, "oversize": 0})
            bucket["total"] += 1
            if _is_bag(record):
                bucket["bags"] += 1
            else:
                bucket["oversize"] += 1

        problem: dict[str, int] = {}
        for record in records:
            state = str(record.get("packageEventState") or "")
            if state and state != "STOWED":
                problem[state] = problem.get(state, 0) + 1

        total = len(stowed)
        return {
            "login": alias,
            "date": date_str,
            "total": total,
            "bags": bags,
            "oversize": oversize,
            "bag_pct": round((bags / total) * 100, 1) if total else 0.0,
            "oversize_pct": round((oversize / total) * 100, 1) if total else 0.0,
            "last_stow": (
                time.strftime("%H:%M", time.localtime(last_ms / 1000)) if last_ms else ""
            ),
            "hourly": [
                {"hour": hour, **counts} for hour, counts in sorted(hourly_map.items())
            ],
            "problem": [
                {"state": state, "count": count}
                for state, count in sorted(problem.items(), key=lambda kv: -kv[1])
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILTER_NS = (
    "http://internal.amazon.com/coral/com.amazon.oculusservice.model.filter/"
)

# Short display codes for the SCC package-size enum (stale packages view).
_SIZE_ABBREVIATIONS = {
    "EXTRA_SMALL": "XS",
    "SMALL": "S",
    "MEDIUM": "M",
    "LARGE": "L",
    "EXTRA_LARGE": "XL",
    "HEAVY_BULKY_NON_CON": "HB",
}


def _find_location_metrics(payload: Any) -> list[dict[str, Any]] | None:
    """Locate the ``locationMetrics`` list anywhere inside the envelope.

    The proxy gateway nests the list at varying depths per deployment, so a
    rigid two-level lookup can silently miss it. Mirrors the tolerant
    recursive search the SCC dashboard's own JS performs: prefer an explicit
    ``locationMetrics`` key at any level, else accept the first list whose
    entries look like induct-line rows (dicts carrying ``locationName``).
    """
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "locationName" in payload[0]:
            return payload
        return None
    if isinstance(payload, dict):
        direct = payload.get("locationMetrics")
        if isinstance(direct, list):
            return direct
        for value in payload.values():
            found = _find_location_metrics(value)
            if found is not None:
                return found
    return None


def _linehaul_hhmm(value: Any) -> str:
    """Extract ``HH:MM`` from an SCC line-haul ISO timestamp.

    The receive board emits local, naive timestamps like
    ``2026-07-15T03:44:00`` (no timezone), so we keep the wall-clock time
    as-is and just pull the ``HH:MM`` out. Empty / missing values yield ``""``.
    """
    text = str(value or "").strip()
    if not text or text.lower() in ("none", "nan"):
        return ""
    m = re.search(r"T(\d{2}):(\d{2})", text)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def _build_filters(term_map: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Build the oculus TermFilter + (empty) RangeFilter list."""
    return [
        {"__type": f"TermFilter:{_FILTER_NS}", "filterMap": term_map},
        {"__type": f"RangeFilter:{_FILTER_NS}", "filterMap": {}},
    ]


def _local_iso_to_epoch(text: Any) -> int | None:
    """Convert an SCC PVA local, naive ISO timestamp to epoch seconds.

    The PVA data emits station-local, timezone-naive stamps like
    ``2026-07-16T01:15``. The toolbox runs on the operator's station machine,
    so interpreting them in the machine's local timezone (``.timestamp()``)
    lines them up with the epoch-second flow windows the front-end builds
    from the same wall-clock hours.
    """
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(str(text)).timestamp())
    except (TypeError, ValueError):
        return None


def _aggregate_dwell_rows(
    rows: list[dict[str, Any]], over_minutes: int | None = None
) -> dict[str, Any]:
    """Summarise dwell drill-down rows into ``{total, aisles[, over_*]}``.

    De-dupes by ``trackingId`` and groups by ``aisle`` (highest first). When
    ``over_minutes`` is given, also counts rows dwelling ``>=`` that many
    minutes (the "90' parcels" figure).
    """
    seen: set[str] = set()
    aisle_counts: dict[str, int] = {}
    total = 0
    over = 0
    for r in rows:
        tid = str(r.get("trackingId") or "").strip().upper()
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        total += 1
        aisle = str(r.get("aisle") or "").strip() or "—"
        aisle_counts[aisle] = aisle_counts.get(aisle, 0) + 1
        if over_minutes is not None:
            mins = r.get("minutesInState")
            try:
                if mins is not None and int(float(mins)) >= over_minutes:
                    over += 1
            except (TypeError, ValueError):
                pass
    aisles = sorted(aisle_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: dict[str, Any] = {
        "total": total,
        "aisles": [{"aisle": a, "count": c} for a, c in aisles],
    }
    if over_minutes is not None:
        out["over_minutes"] = over_minutes
        out["over_count"] = over
    return out


def _pva_points(
    fpva: dict[str, Any], metric_name: str, interval: str
) -> list[tuple[int, float]]:
    """Extract the ``Actual`` ``(epoch_s, value)`` points for a PVA metric.

    Matches on the stable ``metricName`` enum (``IND`` / ``SORTED`` / …) and
    the ``Actual`` task-count type, dropping negative ``-1`` sentinels. Falls
    back to the 60-minute buckets when ``interval`` is missing.
    """
    series_list = fpva.get(interval) or fpva.get("60") or []
    for s in series_list:
        if (
            str(s.get("metricName")) == metric_name
            and str(s.get("taskCountType")) == "Actual"
        ):
            out: list[tuple[int, float]] = []
            for p in s.get("dataPointList", []) or []:
                val = p.get("metricValue")
                ts = p.get("timeStampVal")
                if val is None or ts is None:
                    continue
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue
                if fval < 0:
                    continue
                epoch = _local_iso_to_epoch(ts)
                if epoch is not None:
                    out.append((epoch, fval))
            return out
    return []


def fetch_induct_stow_series(
    *,
    node_id: str | None = None,
    cycle_ids: Iterable[str] = ("CYCLE_1",),
    cookies: dict[str, str] | None = None,
) -> dict[str, list[tuple[int, float]]]:
    """Return the SCC induct (``IND``) + stow (``SORTED``) actual series.

    Convenience wrapper around :meth:`SccClient.fetch_induct_stow_series` so
    the Flow Meeting NS hourly-flow loader doesn't build a client itself.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_induct_stow_series(node_id=node_id, cycle_ids=cycle_ids)


def fetch_at_station_summary(
    *,
    node_id: str | None = None,
    cycles: Iterable[str] = ("CYCLE_1",),
    over_minutes: int = 90,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the SCC "At Station" dwell total + aisle breakdown for a node.

    Convenience wrapper around :meth:`SccClient.fetch_at_station_summary`.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_at_station_summary(
        node_id=node_id, cycles=cycles, over_minutes=over_minutes
    )


def fetch_sideline_summary(
    *,
    node_id: str | None = None,
    cycles: Iterable[str] = ("CYCLE_1",),
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the SCC "Sideline" (Held) cumulative total + aisle breakdown."""
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_sideline_summary(node_id=node_id, cycles=cycles)


def fetch_fsaf_summary(
    *,
    node_id: str | None = None,
    cycles: Iterable[str] = ("CYCLE_1",),
    active_only: bool = True,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the FSAF total + top aisles from the SCC Associate Sort view."""
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_fsaf_summary(
        node_id=node_id, cycles=cycles, active_only=active_only
    )


def fetch_induction_lines(
    *,
    node_id: str | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the live induction-line metrics for a node.

    Convenience wrapper around :meth:`SccClient.fetch_induction_lines`.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_induction_lines(node_id=node_id)


def fetch_finger_metrics(
    *,
    node_id: str | None = None,
    cycles: Iterable[str] = ("CYCLE_1",),
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the per-finger (CLUSTER) stow metrics + station totals.

    Convenience wrapper around :meth:`SccClient.fetch_finger_metrics`.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_finger_metrics(node_id=node_id, cycles=cycles)


def fetch_associates(
    *,
    node_id: str | None = None,
    cycles: Iterable[str] = ("CYCLE_1",),
    cookies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the raw ``associates`` list from the SCC Associate Sort view.

    Convenience wrapper around :meth:`SccClient.fetch_associates` (the
    ``svs/associates/data`` call the Stow > Associate page makes after
    picking a cycle).
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_associates(node_id=node_id, cycles=cycles)


def fetch_pick_associates(
    *,
    node_id: str | None = None,
    date: str | None = None,
    cookies: dict[str, str] | None = None,
) -> list[str]:
    """Return the distinct pick-associate logins from the SCC "Associate
    Pick" dashboard (``/station/dashboard/associatepick``).

    Convenience wrapper around :meth:`SccClient.fetch_pick_associates`.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_pick_associates(node_id=node_id, date=date)


def fetch_retained_summary(
    *,
    node_id: str | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the SCC ageing "Retained / Retenido" row total for a node.

    Convenience wrapper around :meth:`SccClient.fetch_retained_summary`.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_retained_summary(node_id=node_id)


def fetch_stow_wip_summary(
    *,
    node_id: str | None = None,
    cycle: str = "CYCLE_1",
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the STOW-WIP headline (value/compliance/targets/trend).

    Convenience wrapper around :meth:`SccClient.fetch_stow_wip` (the same
    batch the Flow Watch widget uses) so callers don't build a client
    themselves.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_stow_wip(node_id=node_id, cycle=cycle)


def fetch_onroad_rows(
    *,
    node_id: str | None = None,
    variant: str = "both",
    cookies: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return the SCC "On Road" rows, combining both sections by default.

    ``variant`` selects which SCC section(s) to pull: ``failed`` (Ageing
    failed/departed), ``iniciar`` (Received/Inducted "In Transit" at station)
    or ``both`` (default union). Convenience wrapper so callers don't build a
    client themselves.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_onroad_combined(node_id=node_id, variant=variant)


def fetch_cancelled_rows(
    *,
    node_id: str | None = None,
    dwell_max_minutes: int | None = CANCELLED_DWELL_MAX_MINUTES,
    cookies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the raw SCC "Delivery Cancelled" drill-down rows for a node.

    Convenience wrapper around :meth:`SccClient.get_cancelled_drilldown`
    so callers (the Duplicados y Cancelados tool) don't build a client
    themselves. Rows keep the SCC field names (``trackingId`` /
    ``lastUpdatedTime`` / ``routeCode`` / ``cluster`` / ``aisle`` /
    ``sortZone`` / ...); the caller maps them to its own columns.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.get_cancelled_drilldown(
        node_id=node_id, dwell_max_minutes=dwell_max_minutes
    )


def fetch_ead_station_rows(
    *,
    node_id: str | None = None,
    cookies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the SCC rows the EAD / At Station tool consumes.

    Pulls the two dashboard sections the operator used to export as CSVs and
    upload — the "Induct" (In Transit) view and the "Retained" view — reusing
    a single authenticated client. Each row is tagged with its SCC
    ``category`` (the status tile it came from) and a ``source`` of
    ``"induct"`` or ``"retained"`` so the tool can classify it without relying
    on the CSV's positional columns.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    out: list[dict[str, Any]] = []
    for row in client.fetch_categorized_drilldown(
        INDUCT_STATUS_MAP, node_id=node_id, dwell_time=INDUCT_DWELL
    ):
        row["source"] = "induct"
        out.append(row)
    for row in client.fetch_categorized_drilldown(
        RETAINED_STATUS_MAP, node_id=node_id, dwell_time=RETAINED_DWELL
    ):
        row["source"] = "retained"
        out.append(row)
    return out


def fetch_dwell_error_rows(
    *,
    node_id: str | None = None,
    lookback_hours: int = DWELL_DEFAULT_LOOKBACK_HOURS,
    cookies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the raw SCC "Dwell" drill-down rows for a node.

    Convenience wrapper around :meth:`SccClient.fetch_dwell_error_rows` so
    the Dwell / Errors tool doesn't build a client itself. Each row keeps the
    SCC field names (``trackingId`` / ``lastUpdatedTime`` / ``length`` /
    ``sortZone`` / ...) plus the injected ``category`` (dwelling tile).
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_dwell_error_rows(
        node_id=node_id, lookback_hours=lookback_hours
    )


def fetch_pending_depart_rows(
    *,
    node_id: str | None = None,
    cycles: Iterable[str] | None = None,
    lookback_hours: int = DWELL_DEFAULT_LOOKBACK_HOURS,
    cookies: dict[str, str] | None = None,
    only_without_route: bool = True,
) -> list[dict[str, Any]]:
    """Return the SCC "Salida pendiente / Pending Depart" rows for a node.

    Convenience wrapper around :meth:`SccClient.fetch_pending_depart_rows`
    so the Sin Secuencia tool doesn't build a client itself. By default only
    the customer packages **without an assigned route** sourced at our own
    station are returned (the "sin secuencia" set); pass
    ``only_without_route=False`` for the full pending-depart drill-down.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_pending_depart_rows(
        node_id=node_id,
        cycles=cycles,
        lookback_hours=lookback_hours,
        only_without_route=only_without_route,
    )


def fetch_linehaul_rows(
    *,
    node_id: str | None = None,
    cookies: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return the SCC "receive" inbound line-haul rows for a node.

    Convenience wrapper around :meth:`SccClient.fetch_linehaul_rows` so the
    Flow Meeting NS Camiones tool doesn't build a client itself. Each row is
    the ``(vrid / status / origin / dock / planned / actual / volume /
    unloadSeq)`` shape the Camiones tables consume.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_linehaul_rows(node_id=node_id)


def fetch_arrived_linehaul_volume(
    *,
    node_id: str | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, int]:
    """Return the total ``volume`` of raw SCC ``ARRIVED`` line hauls."""
    client = SccClient(cookies=cookies, node_id=node_id)
    return client.fetch_arrived_linehaul_volume(node_id=node_id)


def fetch_package_states(
    tracking_ids: Iterable[str],
    *,
    node_id: str | None = None,
    cookies: dict[str, str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Return ``{tracking_id: currentPackageState}`` for the given IDs.

    Convenience wrapper around :meth:`SccClient.batch_get_package_summary`.
    Tracking IDs are returned upper-cased for stable crossmatching.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    summaries = client.batch_get_package_summary(tracking_ids, progress=progress)
    out: dict[str, str] = {}
    for row in summaries:
        tid = str(row.get("trackingId") or row.get("packageId") or "").strip()
        if not tid:
            continue
        out[tid.upper()] = str(row.get("currentPackageState") or "").strip()
    return out


def fetch_package_details(
    tracking_ids: Iterable[str],
    *,
    node_id: str | None = None,
    cookies: dict[str, str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, dict[str, str]]:
    """Return ``{tracking_id: {"state", "dsp_name", "route"}}`` for IDs.

    Like :func:`fetch_package_states` but also keeping SCC's ``PROVIDER_ID``
    and ``currentRouteCode``. The additive ``route`` field lets consumers
    backfill missing PerfectMile routes without changing existing callers.
    """
    client = SccClient(cookies=cookies, node_id=node_id)
    summaries = client.batch_get_package_summary(tracking_ids, progress=progress)
    out: dict[str, dict[str, str]] = {}
    for row in summaries:
        tid = str(row.get("trackingId") or row.get("packageId") or "").strip()
        if not tid:
            continue
        provider = ""
        add_attrs = row.get("additionalAttributes")
        if isinstance(add_attrs, dict):
            prov = add_attrs.get("PROVIDER_ID")
            if isinstance(prov, dict):
                provider = str(prov.get("value") or "").strip()
        out[tid.upper()] = {
            "state": str(row.get("currentPackageState") or "").strip(),
            "dsp_name": provider or str(row.get("providerId") or "").strip(),
            "route": str(row.get("currentRouteCode") or "").strip(),
        }
    return out


__all__ = [
    "SccAuthError",
    "SccClientError",
    "SCC_LOGIN_URL",
    "SCC_BASE_URL",
    "SccClient",
    "cookies_status",
    "resolve_node_id",
    "fetch_package_states",
    "fetch_package_details",
    "fetch_onroad_rows",
    "fetch_cancelled_rows",
    "fetch_ead_station_rows",
    "fetch_dwell_error_rows",
    "fetch_pending_depart_rows",
    "fetch_linehaul_rows",
    "fetch_arrived_linehaul_volume",
    "fetch_induct_stow_series",
    "fetch_at_station_summary",
    "fetch_sideline_summary",
    "fetch_fsaf_summary",
    "fetch_associates",
    "fetch_retained_summary",
    "DWELL_ERRORS_STATUS_MAP",
    "ONROAD_STATUS_MAP",
    "INICIAR_STATUS_MAP",
    "INICIAR_DWELL",
    "INDUCT_STATUS_MAP",
    "INDUCT_DWELL",
    "RETAINED_STATUS_MAP",
    "RETAINED_DWELL",
    "CANCELLED_STATUS_MAP",
    "CANCELLED_DWELL_MAX_MINUTES",
    "PENDING_DEPART_STATUS_MAP",
]
