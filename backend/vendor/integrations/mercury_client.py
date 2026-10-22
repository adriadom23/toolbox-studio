"""Mercury (mercury.amazon.com) integration helpers.

The legacy Toolbox used to ask the operator to download an Excel from
Mercury and drop it into each tool. With this module the backend
reuses the cookies the user already has in their Firefox profile (same
pattern as DINO Harmony / POD / PerfectMile) and hits the Mercury
``getQueryResponse`` endpoint directly. The HTML table that Mercury
returns is parsed back into a list of dictionaries.

Public API:

* :func:`mercury_login_status` — inspects Firefox cookies and reports
  whether we have everything we need to call Mercury.
* :func:`fetch_query` — runs a query identified by its catalog key and
  returns ``(rows, raw_text, used_cookies)``.
* :func:`parse_mercury_html` — helper that parses the HTML table.

Authentication notes:
* Mercury runs on ``mercury.amazon.com`` and shares the central Amazon
  identity cookies (``amzn_sso_token``, ``session-token``, ...).
* The token is short-lived. We never cache it ourselves; every call
  re-reads the cookies from disk so the session is always fresh.
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import re
from html.parser import HTMLParser
from typing import Any, Iterable

import requests

from app.integrations.firefox_cookies import (
    MERCURY_HOST_PATTERNS,
    MERCURY_REQUIRED_COOKIES,
    has_required,
    read_cookies_from_profile,
)
from app.integrations.mercury_catalog import MercuryQuery, find_query, list_queries


MERCURY_HOST = "mercury.amazon.com"
MERCURY_QUERY_PATH = "/getQueryResponse"
MERCURY_LOGIN_URL = "https://mercury.amazon.com/"
MERCURY_DEFAULT_TIMEOUT = 12.0
# Session probe: (connect, first-byte) — headers only, never the body.
MERCURY_PROBE_TIMEOUT = (5.0, 4.0)
# A Mercury session accepted by the probe is trusted this long; real queries
# still seed an ``auth`` outcome immediately (``_record_query_outcome``).
MERCURY_PROBE_OK_TTL = 600.0


_LOGGER = logging.getLogger(__name__)


# Headers that mirror a real Firefox request (the legacy operator
# captured these). Cookies are added per-request from the cached
# Firefox profile.
#
# Tomcat / the proxy in front of Mercury rejects unusual combinations
# of headers, so we keep this set minimal: just enough to look like a
# normal HTTP/1.1 client. Sec-Fetch-* and Upgrade-Insecure-Requests
# are browser-only context indicators that some corporate proxies
# bounce as 400 when the request isn't navigation-style.
DEFAULT_REQUEST_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,es-ES;q=0.8,es;q=0.5,en;q=0.3",
    "Connection": "keep-alive",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    ),
}


# Cookies we forward to Mercury. Firefox stores hundreds of cookies on
# ``.amazon.com`` (tracking, marketing, ...). Sending all of them
# bloats the Cookie header and lights up Tomcat's HTTP filter — we
# saw 400 responses with the full set. Restrict to the names known to
# matter for Mercury auth + session.
_MERCURY_COOKIE_WHITELIST: tuple[str, ...] = (
    # Single sign-on token (the JWT).
    "amzn_sso_token",
    "amzn_sso_rfp",
    # Amazon session.
    "session-id",
    "session-id-time",
    "session-token",
    "sess-at-main",
    "at-main",
    "x-main",
    "ubid-main",
    "ubid-acbeu",
    "ubid-tacbeu",
    # Tomcat session — set by Mercury itself.
    "JSESSIONID",
    # Locale / region (some endpoints branch on these).
    "i18n-prefs",
    "lc-main",
    "sp-cdn",
)


def _filter_cookies(raw: dict[str, str]) -> dict[str, str]:
    """Return only the auth-relevant cookies."""
    if not raw:
        return {}
    return {name: value for name, value in raw.items() if name in _MERCURY_COOKIE_WHITELIST}


# ---------------------------------------------------------------------------
# Login / cookies
# ---------------------------------------------------------------------------


def _read_mercury_cookies() -> dict[str, str]:
    try:
        return read_cookies_from_profile(
            MERCURY_HOST_PATTERNS,
            target_host=MERCURY_HOST,
        )
    except Exception:
        return {}


def _session_probe_identity(
    query_key: str,
    cookies: dict[str, str],
) -> tuple[str, str]:
    """Return the live-probe source key and auth-cookie fingerprint."""
    from app.integrations.live_session_probe import cookie_fingerprint

    filtered = _filter_cookies(cookies)
    return (
        f"mercury:{query_key}",
        cookie_fingerprint(*(filtered.get(name) for name in _MERCURY_COOKIE_WHITELIST)),
    )


def _record_query_outcome(
    query_key: str,
    cookies: dict[str, str],
    outcome: str,
) -> None:
    """Remember auth observed by a real query for the keyed status probe."""
    try:
        from app.integrations.live_session_probe import record_outcome

        source, fingerprint = _session_probe_identity(query_key, cookies)
        record_outcome(source, fingerprint, outcome)
    except Exception:  # noqa: BLE001 - best-effort cache seed
        pass


def _probe_query_session(
    query: MercuryQuery,
    cookies: dict[str, str],
) -> str:
    """Probe the same non-redirecting Mercury boundary used by extraction."""
    if not query.has_target:
        return "error"
    url = _append_params(_request_url(query), dict(query.request_params or {}))
    return _mercury_probe_status(
        url,
        _filter_cookies(cookies),
        dict(DEFAULT_REQUEST_HEADERS),
        MERCURY_PROBE_TIMEOUT,
        _resolve_verify(None),
    )


def _mercury_probe_status(
    url: str,
    cookies: dict[str, str],
    headers: dict[str, str],
    timeout: float | tuple[float, float],
    verify: bool | str,
) -> str:
    """Session-only check of a Mercury request: status line, never the body.

    Same auth classification as :func:`_mercury_http` (redirect/401/403 =
    ``auth``, >=400 or transport failure = ``error``), but streamed and closed
    right after the headers: the status chip only needs to know whether
    Mercury accepts the session, not the dataset (which can be megabytes).
    """
    try:
        response = requests.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
            verify=verify,
            stream=True,
        )
    except requests.exceptions.ReadTimeout:
        # Connected, but Mercury is still EXECUTING the query: the SSO edge
        # rejects a bad session at once with a redirect/401, so a slow first
        # byte means the session was accepted.
        return "ok"
    except requests.exceptions.RequestException:
        return "error"
    try:
        status = response.status_code
    finally:
        response.close()
    if status in (301, 302, 303, 307, 308, 401, 403):
        return "auth"
    if status >= 400:
        return "error"
    return "ok"


def mercury_login_status(
    query_key: str | None = None,
    *,
    country: str | None = None,
    station: str | None = None,
) -> dict[str, Any]:
    """Return passive cookie status, optionally verified by a real query.

    Calls without ``query_key`` preserve the legacy five-field payload. A
    keyed call reports ready only after Mercury accepts the same
    ``getQueryResponse`` boundary used by extraction; cookie presence alone is
    exposed separately as ``cookies_ready``.
    """
    from app.integrations.firefox_cookies import session_status

    status = session_status(
        MERCURY_HOST_PATTERNS,
        MERCURY_REQUIRED_COOKIES,
        target_host=MERCURY_HOST,
    )
    passive = {
        "ready": status["ready"],
        "missing": status["missing"],
        "expired": status["expired"],
        "cookie_count": status["cookie_count"],
        "token_expired": "amzn_sso_token" in status["expired"],
    }
    key = str(query_key or "").strip()
    if not key:
        return passive

    extended = {
        **passive,
        "ready": False,
        "cookies_ready": bool(passive["ready"]),
        "auth_verified": False,
        "session_rejected": False,
        "reason": "cookies_not_ready" if not passive["ready"] else "",
    }
    if not passive["ready"]:
        return extended

    query = find_query(key, country=country, station=station)
    if query is None or not query.has_target:
        extended["reason"] = "query_not_found"
        return extended

    from app.integrations.live_session_probe import cached_probe

    cookies = _read_mercury_cookies()
    source, fingerprint = _session_probe_identity(key, cookies)
    outcome = cached_probe(
        source,
        fingerprint,
        lambda: _probe_query_session(query, cookies),
        ok_ttl=MERCURY_PROBE_OK_TTL,
    )
    if outcome == "ok":
        extended.update({"ready": True, "auth_verified": True, "reason": ""})
    elif outcome == "auth":
        extended.update(
            {
                "auth_verified": True,
                "session_rejected": True,
                "token_expired": True,
                "reason": "auth_required",
            }
        )
    else:
        extended["reason"] = "network_error"
    return extended


def _sso_token_expired(token: str) -> bool:
    """Cheap JWT exp parser. Returns True when the token has expired.

    We avoid importing PyJWT to keep the dependency surface small;
    the Amazon SSO token is a JWS with a JSON payload at the second
    segment.
    """
    import base64
    import json
    import time

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return False
        payload_b64 = parts[1]
        # JWT uses url-safe base64 without padding.
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return False
    exp = payload.get("exp")
    try:
        exp_int = int(exp) if exp is not None else 0
    except (TypeError, ValueError):
        return False
    if exp_int <= 0:
        return False
    # 30s of grace so we don't bounce a token that's literally about to refresh.
    return exp_int < (time.time() - 30)


def has_mercury_cookies(cookies: dict[str, str] | None = None) -> bool:
    return has_required(cookies or _read_mercury_cookies(), MERCURY_REQUIRED_COOKIES)


def describe_request(
    query_key: str,
    *,
    country: str | None = None,
    station: str | None = None,
) -> dict[str, Any]:
    """Return what ``fetch_query`` would send (debug helper).

    Cookie values are masked (only first / last 4 chars) so the
    payload is safe to log or echo to the frontend.
    """
    query = find_query(query_key, country=country, station=station)
    if query is None:
        return {"error": f"unknown query '{query_key}'"}
    cookies = _filter_cookies(_read_mercury_cookies())

    def _mask(value: str) -> str:
        text = str(value or "")
        if len(text) <= 8:
            return "*" * len(text)
        return f"{text[:4]}...{text[-4:]} ({len(text)} chars)"

    return {
        "url": _request_url(query),
        "headers": dict(DEFAULT_REQUEST_HEADERS),
        "cookies": {name: _mask(value) for name, value in cookies.items()},
        "cookie_count": len(cookies),
        "missing": [name for name in MERCURY_REQUIRED_COOKIES if name not in cookies],
    }


# ---------------------------------------------------------------------------
# HTML parser (no BeautifulSoup dependency)
# ---------------------------------------------------------------------------


class _TableParser(HTMLParser):
    """Extract the first ``<table>`` of a Mercury response into rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_table = False
        self._inside_row = False
        self._inside_cell = False
        self._cell_kind = ""  # "th" or "td"
        self._cell_buffer: list[str] = []
        self._row: list[str] = []
        self.headers: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs):  # noqa: ARG002
        tag = tag.lower()
        if tag == "table":
            self._inside_table = True
            return
        if not self._inside_table:
            return
        if tag == "tr":
            self._inside_row = True
            self._row = []
        elif tag in ("td", "th"):
            self._inside_cell = True
            self._cell_kind = tag
            self._cell_buffer = []
        elif tag == "br" and self._inside_cell:
            self._cell_buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self._inside_table = False
            return
        if not self._inside_table:
            return
        if tag in ("td", "th") and self._inside_cell:
            self._inside_cell = False
            value = html.unescape("".join(self._cell_buffer)).strip()
            if self._cell_kind == "th":
                self.headers.append(value)
            else:
                self._row.append(value)
            self._cell_buffer = []
            self._cell_kind = ""
        elif tag == "tr" and self._inside_row:
            self._inside_row = False
            if self._row:
                self.rows.append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._inside_cell:
            self._cell_buffer.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._inside_cell:
            self._cell_buffer.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self._inside_cell:
            self._cell_buffer.append(html.unescape(f"&#{name};"))


_TRANSPORTER_RE = re.compile(r'"transporterId"\s*:\s*"([A-Z0-9]+)"')


def _flatten_transporter(value: str) -> str:
    """Mercury returns ``[{"transporterId":"A1..."}]``. Keep the first id."""
    if not value:
        return ""
    match = _TRANSPORTER_RE.search(value)
    return match.group(1) if match else value


# ---------------------------------------------------------------------------
# Format-agnostic table extraction
# ---------------------------------------------------------------------------
#
# What matters is the DATA the configured link returns, not how the link
# is built. Mercury has served the same saved query as an HTML table
# (``getQueryResponse``), and newer "Refreshable Excel" links
# (``getWebQueryData?metricId=``) may answer with JSON or CSV. Every
# consumer goes through :func:`parse_mercury_table`, which sniffs the
# body and returns ``(headers, positional rows)`` for any of them.

_JSON_ROW_KEYS = ("rows", "data", "values", "records", "items", "results", "result", "hits")
_JSON_HEADER_KEYS = ("columns", "headers", "columnNames", "header", "fields")


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _column_name(col: Any) -> str:
    if isinstance(col, dict):
        for key in ("name", "label", "field", "key", "id", "title", "displayName"):
            if col.get(key):
                return str(col[key]).strip()
        return ""
    return _cell_text(col)


def _records_to_table(records: list[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    flat = [_flatten_source(rec) for rec in records if isinstance(rec, dict)]
    headers: list[str] = []
    seen: set[str] = set()
    for rec in flat:
        for key in rec:
            if key not in seen:
                seen.add(key)
                headers.append(key)
    return headers, [[_cell_text(rec.get(h)) for h in headers] for rec in flat]


def _table_from_json(data: Any, depth: int = 0) -> tuple[list[str], list[list[str]]]:
    if depth > 4:
        return [], []
    if isinstance(data, list):
        if not data:
            return [], []
        if all(isinstance(item, dict) for item in data):
            # ES-style hits keep the payload under ``_source``.
            if all("_source" in item for item in data):
                data = [item.get("_source") or {} for item in data]
            return _records_to_table(data)
        if all(isinstance(item, list) for item in data):
            header = [_cell_text(c) for c in data[0]]
            return header, [[_cell_text(c) for c in row] for row in data[1:]]
        return [], []
    if isinstance(data, dict):
        header_val = next((data[k] for k in _JSON_HEADER_KEYS if isinstance(data.get(k), list)), None)
        rows_val = next((data[k] for k in _JSON_ROW_KEYS if k in data), None)
        if header_val is not None and isinstance(rows_val, list):
            headers = [_column_name(c) for c in header_val]
            if rows_val and all(isinstance(r, dict) for r in rows_val):
                return headers, [[_cell_text(r.get(h)) for h in headers] for r in rows_val]
            return headers, [[_cell_text(c) for c in r] for r in rows_val if isinstance(r, list)]
        if rows_val is not None:
            headers, rows = _table_from_json(rows_val, depth + 1)
            if headers:
                return headers, rows
        for value in data.values():
            if isinstance(value, (list, dict)):
                headers, rows = _table_from_json(value, depth + 1)
                if headers:
                    return headers, rows
    return [], []


def parse_mercury_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Return ``(headers, rows)`` from a Mercury response of ANY format.

    Supports the HTML table (first ``<table>``), JSON (list of records,
    ``{columns, rows}`` shapes, nested ``data``/``rows``/ES ``hits``) and
    CSV/TSV/semicolon exports. Unknown bodies yield ``([], [])``.
    """
    body = (text or "").lstrip("\ufeff").strip()
    if not body:
        return [], []
    if body[0] in "[{":
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if data is not None:
            return _table_from_json(data)
    head = body[:4096].lower()
    if body.startswith("<") or "<table" in head or "<html" in head:
        parser = _TableParser()
        parser.feed(body)
        parser.close()
        return list(parser.headers), [list(row) for row in parser.rows]
    sample = body[:4096]
    delimiter = max((",", ";", "\t"), key=sample.count)
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    lines = [row for row in reader if any(cell.strip() for cell in row)]
    if not lines or len(lines[0]) < 2:
        return [], []
    return [h.strip() for h in lines[0]], [[c.strip() for c in row] for row in lines[1:]]


def parse_mercury_html(text: str, *, column_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Return the parsed rows of a Mercury response as header-keyed dicts.

    Despite the historical name the body may be HTML, JSON or CSV (see
    :func:`parse_mercury_table`). When ``column_map`` is provided each
    header is renamed to the mapped value (e.g. ``"trackingId" ->
    "tracking_id"``). Headers that don't appear in ``column_map`` are
    emitted verbatim.

    Cells whose value looks like a JSON list of transporter objects
    are flattened to the first ``transporterId``.
    """
    headers, rows = parse_mercury_table(text)
    if not headers:
        return []

    mapping = {k: v for k, v in (column_map or {}).items() if k and v}
    out: list[dict[str, Any]] = []
    for raw_row in rows:
        # Pad short rows so zip preserves the column alignment.
        if len(raw_row) < len(headers):
            raw_row = raw_row + [""] * (len(headers) - len(raw_row))
        record: dict[str, Any] = {}
        for header, cell in zip(headers, raw_row):
            key = mapping.get(header, header)
            value = cell.strip()
            if "transporter" in key.lower() and value.startswith("["):
                value = _flatten_transporter(value)
            record[key] = value
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


class MercuryAuthError(RuntimeError):
    """Raised when the user has no Mercury session cookies."""


class MercuryRequestError(RuntimeError):
    """Raised when Mercury responds with a non-2xx status."""


class MercuryNetworkError(RuntimeError):
    """Raised when the request to Mercury fails before reaching it.

    Typical causes: VPN off, DNS not resolvable, timeouts, captive
    portal. We surface the cause so the operator knows what to fix
    instead of seeing a generic "NetworkError" bubble up.
    """


def _build_request_url(query_id: str, region: str) -> str:
    safe_region = (region or "eu").strip().lower() or "eu"
    return f"https://{MERCURY_HOST}{MERCURY_QUERY_PATH}?ID={query_id}&region={safe_region}"


def _request_url(query: MercuryQuery) -> str:
    """URL to fetch for ``query``.

    The station's pasted link wins and is used AS-IS (path + params),
    whatever its style. Only catalog-only queries rebuild the classic
    ``getQueryResponse?ID=`` request from their id.
    """
    if query.source_url:
        return query.source_url
    return _build_request_url(query.query_id, query.region)


def _apply_node_filter(
    rows: list[dict[str, Any]],
    query: MercuryQuery,
    station: str | None,
) -> list[dict[str, Any]]:
    """Keep only rows whose node matches the requested station.

    Some Mercury dashboards (e.g. the Handover RTS query) return rows
    for every node in the region. When the query declares a
    ``node_filter`` block we drop everything that does not belong to
    the configured station::

        "node_filter": {
            "column": "node_id",
            "raw_columns": ["compLastScanInOrder.source.nodeId"]
        }

    ``column`` is the canonical (post ``column_map``) key; ``raw_columns``
    cover the un-mapped header variants so the filter works whether or
    not the rename was applied. The target node defaults to the query's
    catalog station when the caller does not pin one (or asks for the
    ``__ALL__`` sentinel).
    """
    node_filter = query.node_filter or {}
    if not node_filter:
        return rows

    candidates: list[str] = []
    column = str(node_filter.get("column") or "").strip()
    if column:
        candidates.append(column)
    for raw in node_filter.get("raw_columns") or []:
        raw_key = str(raw).strip()
        if raw_key and raw_key not in candidates:
            candidates.append(raw_key)
    # Cover every raw header that ``column_map`` renames to the canonical
    # column so the filter keeps working before/after the rename.
    for raw_key, mapped in (query.column_map or {}).items():
        if mapped in candidates and raw_key not in candidates:
            candidates.append(raw_key)
    if not candidates:
        return rows

    target = (station or "").strip().upper()
    if not target or target == "__ALL__":
        target = (query.station or "").strip().upper()
    if not target:
        return rows

    filtered: list[dict[str, Any]] = []
    for row in rows:
        key = next((k for k in candidates if k in row), None)
        if key is None:
            # The per-DS query does not return the node column (some
            # configured dashboards omit it). We cannot filter on a
            # missing column — keep the row instead of silently
            # dropping every result.
            filtered.append(row)
            continue
        if str(row.get(key) or "").strip().upper() == target:
            filtered.append(row)
    return filtered


def _fetch_query_impl(
    query_key: str,
    *,
    country: str | None = None,
    station: str | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = MERCURY_DEFAULT_TIMEOUT,
    extra_params: dict[str, str] | None = None,
    verify: bool | str | None = None,
) -> tuple[list[dict[str, Any]], str, MercuryQuery]:
    """Run a Mercury query and return ``(rows, raw_text, query_def)``.

    Reads cookies from the user's Firefox profile when not provided.
    Raises :class:`MercuryAuthError` if no usable session exists, and
    :class:`MercuryRequestError` for HTTP failures.

    ``verify`` controls TLS verification. ``None`` (default) honours
    the OS trust store via ``truststore`` (injected at app boot). To
    bypass corporate proxies that we cannot trust automatically, set
    ``MERCURY_VERIFY_TLS=0`` in the environment or pass ``verify=False``
    here.
    """
    query = find_query(query_key, country=country, station=station)
    if query is None:
        raise LookupError(
            f"Mercury query '{query_key}' no esta definida para country={country} station={station}."
        )

    # Queries backed by the Mercury Elasticsearch ``_search`` API page
    # through every hit (the legacy getQueryResponse HTML caps at 1000).
    #
    # The ES proxy host (``mercury-webserver-*.proxy.amazon.com``) sits
    # behind its own Midway gate that we cannot always re-establish from
    # the c3 SSO cookies — the operator ends up logged into Mercury yet
    # the _search call still 307s to midway-auth. When that happens, and
    # the query also declares a ``getQueryResponse`` ``id``, fall back to
    # the HTML endpoint: it authenticates with the very same cookies every
    # other (working) query uses and returns the same dataset (capped at
    # the configured pagination page size).
    if query.search:
        try:
            rows, raw_text, q = fetch_search_query(
                query, cookies=cookies, timeout=timeout, verify=verify
            )
            return _apply_node_filter(rows, query, station), raw_text, q
        except (MercuryAuthError, MercuryNetworkError, MercuryRequestError) as exc:
            if not query.has_target:
                raise
            _LOGGER.warning(
                "Mercury _search fallo para '%s' (%s); usando %s.",
                query.key,
                exc,
                _request_url(query),
            )
            # Fall through to the getQueryResponse path below.

    session_cookies = cookies if cookies is not None else _read_mercury_cookies()
    if not has_required(session_cookies, MERCURY_REQUIRED_COOKIES):
        raise MercuryAuthError(
            "No hay cookies de Mercury (mercury.amazon.com). Inicia sesion en el navegador."
        )

    # Drop the dozens of non-auth cookies Firefox stores on
    # ``.amazon.com``. Tomcat in front of Mercury bounces requests
    # with very large Cookie headers as HTTP 400.
    session_cookies = _filter_cookies(session_cookies)
    if not has_required(session_cookies, MERCURY_REQUIRED_COOKIES):
        raise MercuryAuthError(
            "Cookies Mercury filtradas pero faltan las requeridas. Vuelve a iniciar sesion."
        )

    url = _request_url(query)
    headers = dict(DEFAULT_REQUEST_HEADERS)
    # ``Host`` is added by requests/urllib3 automatically. Setting it
    # manually causes Tomcat to bounce the request with HTTP 400 due
    # to duplicate Host headers, so we leave it out.

    if verify is None:
        env_verify = os.environ.get("MERCURY_VERIFY_TLS", "").strip().lower()
        if env_verify in ("0", "false", "no", "off"):
            verify = False
        else:
            verify = True

    # Base extra params: ``request_params`` declared on the query
    # (e.g. ``{"size": "1000"}``) merged with explicit ``extra_params``.
    base_params: dict[str, str] = {}
    if query.request_params:
        base_params.update(query.request_params)
    if extra_params:
        base_params.update(extra_params)

    # ------------------------------------------------------------------
    # Pagination. Mercury's ``getQueryResponse`` caps each response at a
    # fixed page size (~1000 rows). To pull a whole dashboard we page
    # through it with an offset parameter. The offset parameter name
    # differs per backend and isn't documented here, so we AUTO-DETECT
    # it: we request page 2 with each candidate name and keep the one
    # whose response is mostly *new* rows (i.e. the offset was honoured).
    # Candidates / page size live in the catalog (``pagination`` block)
    # so they can be tuned without code changes. If none work we fall
    # back to the first page (no infinite loop, no duplicates).
    # ------------------------------------------------------------------
    pagination = query.pagination or {}
    if pagination:
        rows, raw_text, q = _fetch_all_paginated(
            url,
            query,
            session_cookies,
            headers,
            timeout,
            verify,
            base_params,
            pagination,
        )
        return _apply_node_filter(rows, query, station), raw_text, q

    page_url = _append_params(url, base_params)
    text = _mercury_http_get(page_url, session_cookies, headers, timeout, verify)
    rows = parse_mercury_html(text, column_map=query.column_map)
    return _apply_node_filter(rows, query, station), text, query


def fetch_query(
    query_key: str,
    *,
    country: str | None = None,
    station: str | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = MERCURY_DEFAULT_TIMEOUT,
    extra_params: dict[str, str] | None = None,
    verify: bool | str | None = None,
) -> tuple[list[dict[str, Any]], str, MercuryQuery]:
    """Run a catalog query and synchronize its auth result with status."""
    session_cookies = cookies if cookies is not None else _read_mercury_cookies()
    try:
        result = _fetch_query_impl(
            query_key,
            country=country,
            station=station,
            cookies=session_cookies,
            timeout=timeout,
            extra_params=extra_params,
            verify=verify,
        )
    except MercuryAuthError:
        _record_query_outcome(query_key, session_cookies, "auth")
        raise
    else:
        _record_query_outcome(query_key, session_cookies, "ok")
        return result


def _resolve_verify(verify: bool | str | None) -> bool | str:
    """Resolve the TLS-verify flag honouring ``MERCURY_VERIFY_TLS``."""
    if verify is not None:
        return verify
    env_verify = os.environ.get("MERCURY_VERIFY_TLS", "").strip().lower()
    if env_verify in ("0", "false", "no", "off"):
        return False
    return True


def fetch_html_table_by_id(
    query_id: str,
    *,
    region: str = "eu",
    cookies: dict[str, str] | None = None,
    timeout: float = MERCURY_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> tuple[list[str], list[list[str]], str]:
    """Fetch a Mercury ``getQueryResponse`` query by its raw ID / region.

    Unlike :func:`fetch_query` (which resolves a catalog entry and
    returns header-keyed dicts), this helper takes the query ID
    directly and returns the table **positionally**:
    ``(headers, rows, raw_text)`` where each row is a list of cell
    strings in column order. Use it when the consumer needs index-based
    access to columns (e.g. "the last column is the site") instead of
    the catalog ``column_map``.

    Reads cookies from the user's Firefox profile when not provided.
    Raises :class:`MercuryAuthError` if no usable session exists, and
    :class:`MercuryRequestError` / :class:`MercuryNetworkError` for
    HTTP / network failures.
    """
    ident = (query_id or "").strip()
    if not ident:
        raise MercuryRequestError("Falta el ID de la query Mercury.")
    return fetch_table_by_url(
        _build_request_url(ident, region),
        cookies=cookies,
        timeout=timeout,
        verify=verify,
    )


def fetch_table_for_query(
    query: MercuryQuery,
    *,
    cookies: dict[str, str] | None = None,
    timeout: float = MERCURY_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> tuple[list[str], list[list[str]], str]:
    """Positional table for a catalog query (pasted link as-is, else id)."""
    if not query.has_target:
        raise MercuryRequestError("Falta el link/ID de la query Mercury.")
    return fetch_table_by_url(_request_url(query), cookies=cookies, timeout=timeout, verify=verify)


def fetch_table_by_url(
    url: str,
    *,
    cookies: dict[str, str] | None = None,
    timeout: float = MERCURY_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> tuple[list[str], list[list[str]], str]:
    """Fetch ANY Mercury link (as configured) and return its table.

    Follows the Midway chain like :func:`fetch_html_table_by_id` and
    parses the body whatever its format (HTML / JSON / CSV, see
    :func:`parse_mercury_table`). Returns ``(headers, rows, raw_text)``.
    """
    from app.integrations.mercury_catalog import normalize_mercury_url

    url = normalize_mercury_url(url)
    if not url:
        raise MercuryRequestError("Link de Mercury no válido.")

    session_cookies = _filter_cookies(
        cookies if cookies is not None else _read_mercury_cookies()
    )
    verify = _resolve_verify(verify)

    # Mercury is gated by Midway. A Mercury-specific ``amzn_sso_token`` may
    # expire while the operator's Midway session is still valid; in that case
    # the redirect chain mints a fresh token. Do not reject before attempting
    # that recovery (the old guard made Flow NS stay red indefinitely).
    try:
        from app.integrations.firefox_cookies import midway_cookies_from_profile

        midway_cookies = midway_cookies_from_profile() or {}
    except Exception:  # noqa: BLE001
        midway_cookies = {}

    if not has_required(session_cookies, MERCURY_REQUIRED_COOKIES) and not midway_cookies.get("session"):
        raise MercuryAuthError(
            "No hay sesión Mercury ni Midway utilizable. Inicia sesión en Firefox."
        )

    sess = requests.Session()
    jar = dict(session_cookies)
    if midway_cookies.get("session"):
        jar.setdefault("session", midway_cookies["session"])
    for name, value in jar.items():
        try:
            sess.cookies.set(name, value, domain=".amazon.com")
        except Exception:  # noqa: BLE001
            pass

    nav_headers = dict(DEFAULT_REQUEST_HEADERS)
    nav_headers.update(
        {
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Upgrade-Insecure-Requests": "1",
        }
    )

    try:
        try:
            resp = sess.get(
                url,
                headers=nav_headers,
                timeout=timeout,
                verify=verify,
                allow_redirects=True,
            )
        except requests.exceptions.Timeout as exc:
            raise MercuryNetworkError(
                f"Mercury no respondio en {timeout:.0f}s. Revisa la VPN o que mercury.amazon.com sea accesible."
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise MercuryNetworkError(
                "Error SSL con mercury.amazon.com (proxy corporativo). "
                "Confirma el root cert de Amazon o usa MERCURY_VERIFY_TLS=0. "
                f"Detalle: {exc}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise MercuryNetworkError(
                f"No se pudo conectar. Revisa la VPN/red corporativa. ({exc.__class__.__name__})"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise MercuryNetworkError(f"Error de red contra Mercury: {exc}") from exc

        from urllib.parse import urlparse

        final_host = (urlparse(resp.url).hostname or "").lower()
        if "midway" in final_host or resp.status_code in (401, 403):
            raise MercuryAuthError(
                "Mercury pide login Midway. Abre mercury.amazon.com en Firefox, inicia "
                "sesion (Midway) y vuelve a pulsar Extraer."
            )
        if resp.status_code >= 400:
            raise MercuryRequestError(
                f"Mercury respondio HTTP {resp.status_code}. Body: {resp.text[:200]}"
            )

        text = resp.text or ""
        headers, rows = parse_mercury_table(text)
        return headers, rows, text
    finally:
        # Always release the underlying connection so we don't leak the
        # Mercury/C3 session between fetches.
        sess.close()


def _append_params(url: str, params: dict[str, str]) -> str:
    """Merge ``params`` into ``url``'s query string (later values win).

    Works for any pasted link (with or without ``?``) and never
    duplicates a key the link already carries.
    """
    if not params:
        return url
    from urllib.parse import parse_qsl, urlencode, urlparse

    parsed = urlparse(url)
    merged = dict(parse_qsl(parsed.query, keep_blank_values=True))
    merged.update({str(k): str(v) for k, v in params.items()})
    return parsed._replace(query=urlencode(merged, doseq=True)).geturl()


def _mercury_http_get(
    url: str,
    cookies: dict[str, str],
    headers: dict[str, str],
    timeout: float,
    verify: bool | str,
) -> str:
    """Single GET to Mercury with the shared error handling. Returns text."""
    return _mercury_http(url, cookies, headers, timeout, verify, method="GET")


def _mercury_http(
    url: str,
    cookies: dict[str, str],
    headers: dict[str, str],
    timeout: float,
    verify: bool | str,
    *,
    method: str = "GET",
    json_body: Any = None,
) -> str:
    """Single GET/POST to Mercury with the shared error handling. Returns text."""
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
            verify=verify,
            json=json_body,
        )
    except requests.exceptions.Timeout as exc:
        raise MercuryNetworkError(
            f"Mercury no respondio en {timeout:.0f}s. Revisa la VPN o que mercury.amazon.com sea accesible."
        ) from exc
    except requests.exceptions.SSLError as exc:
        raise MercuryNetworkError(
            "Error SSL con mercury.amazon.com (proxy corporativo). "
            "Confirma que tu maquina tiene instalado el root cert de Amazon, "
            "o desactiva la verificacion con MERCURY_VERIFY_TLS=0. "
            f"Detalle: {exc}"
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise MercuryNetworkError(
            f"No se pudo conectar. Revisa la VPN/red corporativa. ({exc.__class__.__name__})"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise MercuryNetworkError(f"Error de red contra Mercury: {exc}") from exc
    if response.status_code in (301, 302, 303, 307, 308, 401, 403):
        location = response.headers.get("Location", "")
        raise MercuryAuthError(
            f"Mercury redirige/rechaza la sesión (HTTP {response.status_code} -> {location[:120]}). "
            "Vuelve a iniciar sesion."
        )
    if response.status_code >= 400:
        raise MercuryRequestError(
            f"Mercury respondio HTTP {response.status_code}. Body: {response.text[:200]}"
        )
    return response.text or ""


def _is_empty_leaf(value: Any) -> bool:
    """A leaf is "empty" when it carries no usable text."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _flatten_source(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested ES ``_source`` into dotted keys.

    ``{"LMTRUpdates": {"PTRAS": {"dspShortCode": "TDSL"}}}`` becomes
    ``{"LMTRUpdates.PTRAS.dspShortCode": "TDSL"}`` so the catalog
    ``column_map`` (which uses dotted paths) can rename them.

    Lists are flattened too: the transport documents Mercury returns
    keep ``LMTRUpdates.PTRAS`` / ``LMTRUpdates.PTRS`` as **arrays** of
    update objects, so descending only into dicts (the previous
    behaviour) left those nodes as raw lists and the dotted leaves
    (``...dspShortCode``, ``...scannableId``, ...) never appeared.
    When several array entries map to the same dotted key we keep the
    last non-empty value, which mirrors picking the most recent update.
    """
    out: dict[str, Any] = {}

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for item in node:
                _walk(item, path)
        else:
            if not path:
                return
            if path not in out or not _is_empty_leaf(node):
                out[path] = node

    _walk(obj, prefix)
    return out


def _es_total(hits: dict[str, Any]) -> int:
    total = hits.get("total")
    if isinstance(total, dict):  # ES7+ shape: {"value": N, "relation": "eq"}
        try:
            return int(total.get("value") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(total or 0)
    except (TypeError, ValueError):
        return 0


def _read_search_cookies(host: str) -> dict[str, str]:
    """Read every Firefox cookie scoped to ``host`` (and its parent
    domains). The Mercury ES proxy authenticates via Midway and keeps
    its own session cookies on that host — the c3 cookie set is not
    enough, so we read the proxy host's cookies directly.
    """
    host = (host or "").lower().strip()
    if not host:
        return {}
    patterns: list[str] = [host, f".{host}"]
    parts = host.split(".")
    # Add parent-domain patterns (e.g. .dub.proxy.amazon.com, .proxy.amazon.com, .amazon.com)
    for idx in range(1, len(parts) - 1):
        patterns.append("." + ".".join(parts[idx:]))
    patterns.append(".amazon.com")
    seen: list[str] = []
    for pat in patterns:
        if pat not in seen:
            seen.append(pat)
    try:
        return read_cookies_from_profile(tuple(seen), target_host=host)
    except Exception:  # noqa: BLE001
        return {}


def fetch_search_query(
    query: MercuryQuery,
    *,
    cookies: dict[str, str] | None = None,
    timeout: float = MERCURY_DEFAULT_TIMEOUT,
    verify: bool | str | None = None,
) -> tuple[list[dict[str, Any]], str, MercuryQuery]:
    """Fetch a query backed by the Mercury Elasticsearch ``_search`` API.

    The dashboard's table data comes from a POST to
    ``mercury-webserver-*/<index>/_search`` with an ES query body and
    native ``from``/``size`` pagination (the legacy ``getQueryResponse``
    HTML view caps at 1000 rows). We page through every hit, flatten the
    nested ``_source`` and rename columns via the catalog ``column_map``.
    """
    from urllib.parse import urlencode, urlparse

    search = query.search or {}
    base_url = str(search.get("url") or "").strip()
    if not base_url:
        raise MercuryRequestError("La query no define 'search.url'.")

    search_host = urlparse(base_url).hostname or MERCURY_HOST
    # The ES proxy host has its own Midway session cookies — read those
    # (plus the shared .amazon.com SSO cookies) and send them UNFILTERED.
    if cookies is not None:
        session_cookies = dict(cookies)
    else:
        session_cookies = _read_search_cookies(search_host)
    if not has_required(session_cookies, MERCURY_REQUIRED_COOKIES):
        raise MercuryAuthError(
            "No hay cookies de Mercury (mercury.amazon.com). Inicia sesion en el navegador."
        )

    # The proxy gateway authenticates via Midway (it 307s to
    # midway-auth.amazon.com when unauthenticated). We add the Midway
    # ``session`` cookie and let a requests.Session follow the OIDC
    # redirect chain to mint the proxy session cookie, exactly like the
    # browser navigation does.
    try:
        from app.integrations.firefox_cookies import midway_cookies_from_profile

        midway_cookies = midway_cookies_from_profile() or {}
    except Exception:  # noqa: BLE001
        midway_cookies = {}

    if verify is None:
        env_verify = os.environ.get("MERCURY_VERIFY_TLS", "").strip().lower()
        verify = not (env_verify in ("0", "false", "no", "off"))

    query_params = {str(k): str(v) for k, v in (search.get("query_params") or {}).items()}
    url = f"{base_url}?{urlencode(query_params)}" if query_params else base_url
    dashboard_url = query_params.get("dashboardUrl", "https://mercury.amazon.com/")

    # Build a session whose jar carries every cookie on ``.amazon.com``
    # (covers c3, the proxy host and midway-auth) so redirects across
    # those hosts stay authenticated.
    sess = requests.Session()
    jar_cookies = dict(session_cookies)
    if midway_cookies.get("session"):
        jar_cookies.setdefault("session", midway_cookies["session"])
    for name, value in jar_cookies.items():
        try:
            sess.cookies.set(name, value, domain=".amazon.com")
        except Exception:  # noqa: BLE001
            pass

    base_headers = dict(DEFAULT_REQUEST_HEADERS)
    base_headers.update({"Origin": "https://mercury.amazon.com", "Referer": dashboard_url})
    post_headers = dict(base_headers)
    post_headers.update(
        {
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json, text/plain, */*",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
    )

    # 1) Establish the proxy OIDC session. A navigation-style GET to the
    #    endpoint triggers the gateway's Midway redirect; requests follows
    #    it with the Midway session cookie and the proxy sets its session.
    nav_headers = dict(base_headers)
    nav_headers.update(
        {
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    try:
        sess.get(url, headers=nav_headers, timeout=timeout, verify=verify, allow_redirects=True)
    except requests.exceptions.RequestException:
        pass  # establishment is best-effort; the POST below reports auth issues

    body_template = dict(search.get("body") or {})
    try:
        page_size = int(search.get("page_size") or 1000)
    except (TypeError, ValueError):
        page_size = 1000
    page_size = max(1, page_size)
    try:
        max_pages = int(search.get("max_pages") or 20)
    except (TypeError, ValueError):
        max_pages = 20
    max_pages = max(1, max_pages)

    column_map = query.column_map or {}
    all_rows: list[dict[str, Any]] = []
    last_text = ""
    offset = 0
    total: int | None = None

    for _page in range(max_pages):
        body = dict(body_template)
        body["size"] = page_size
        body["from"] = offset
        try:
            resp = sess.post(
                url,
                headers=post_headers,
                json=body,
                timeout=timeout,
                verify=verify,
                allow_redirects=True,
            )
        except requests.exceptions.Timeout as exc:
            raise MercuryNetworkError(
                f"Mercury _search no respondio en {timeout:.0f}s. Revisa la VPN."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise MercuryNetworkError(f"Error de red contra Mercury _search: {exc}") from exc

        final_host = ""
        try:
            from urllib.parse import urlparse as _urlparse

            final_host = (_urlparse(resp.url).hostname or "").lower()
        except Exception:  # noqa: BLE001
            final_host = ""
        if "midway" in final_host or resp.status_code in (401, 403):
            raise MercuryAuthError(
                "El proxy de Mercury pide login Midway. Abre el dashboard "
                "(mercury.amazon.com/eu/mercury_amzl) en Firefox para iniciar sesion y reintenta."
            )
        if resp.status_code >= 400:
            raise MercuryRequestError(
                f"Mercury _search respondio HTTP {resp.status_code}: {resp.text[:200]}"
            )

        last_text = resp.text or ""
        try:
            data = json.loads(last_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise MercuryRequestError(
                f"Respuesta de Mercury _search no es JSON: {str(exc)[:120]}"
            ) from exc
        hits_block = data.get("hits", {}) if isinstance(data, dict) else {}
        if total is None:
            total = _es_total(hits_block)
        page_hits = hits_block.get("hits", []) or []
        if not page_hits:
            break
        for hit in page_hits:
            flat = _flatten_source(hit.get("_source", {}))
            row = {column_map.get(k, k): v for k, v in flat.items()}
            all_rows.append(row)
        offset += len(page_hits)
        if len(page_hits) < page_size:
            break
        if total is not None and offset >= total:
            break

    return all_rows, last_text, query


def _row_signature(row: dict[str, Any]) -> str:
    return repr(sorted(row.items()))


# Default offset parameter names tried when the catalog does not pin
# one. ``from``/``offset`` are the Elasticsearch conventions; the rest
# cover common alternatives. Page-number style params are handled
# separately below.
_DEFAULT_OFFSET_CANDIDATES: tuple[str, ...] = (
    "offset",
    "from",
    "start",
    "startIndex",
    "firstResult",
    "skip",
    "fromIndex",
)
_DEFAULT_PAGE_CANDIDATES: tuple[str, ...] = (
    "page",
    "pageNumber",
    "pageNum",
    "pageIndex",
)


def _fetch_all_paginated(
    url: str,
    query: "MercuryQuery",
    cookies: dict[str, str],
    headers: dict[str, str],
    timeout: float,
    verify: bool | str,
    base_params: dict[str, str],
    pagination: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, "MercuryQuery"]:
    """Page through ``getQueryResponse`` collecting every row.

    Auto-detects the offset parameter by probing page 2 with several
    candidate names and keeping the one that returns mostly new rows.
    Falls back to the first page when no candidate paginates.
    """
    size_param = str(pagination.get("size_param") or "size").strip()
    try:
        page_size = int(pagination.get("page_size") or 1000)
    except (TypeError, ValueError):
        page_size = 1000
    page_size = max(1, page_size)
    try:
        max_pages = int(pagination.get("max_pages") or 50)
    except (TypeError, ValueError):
        max_pages = 50
    max_pages = max(1, max_pages)

    explicit = str(pagination.get("offset_param") or "").strip()
    offset_candidates = (
        [explicit]
        if explicit
        else [str(c) for c in (pagination.get("offset_candidates") or _DEFAULT_OFFSET_CANDIDATES) if str(c)]
    )
    page_candidates = (
        []
        if explicit
        else [str(c) for c in (pagination.get("page_candidates") or _DEFAULT_PAGE_CANDIDATES) if str(c)]
    )

    def fetch_page(extra: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
        params = dict(base_params)
        if size_param:
            params[size_param] = str(page_size)
        params.update(extra)
        text = _mercury_http_get(_append_params(url, params), cookies, headers, timeout, verify)
        return text, parse_mercury_html(text, column_map=query.column_map)

    seen: set[str] = set()
    all_rows: list[dict[str, Any]] = []

    def add_rows(rows: list[dict[str, Any]]) -> int:
        added = 0
        for row in rows:
            sig = _row_signature(row)
            if sig in seen:
                continue
            seen.add(sig)
            all_rows.append(row)
            added += 1
        return added

    first_text, first_rows = fetch_page({})
    add_rows(first_rows)
    last_text = first_text
    if len(first_rows) < page_size:
        return all_rows, last_text, query  # everything fit on one page

    # Probe candidates on "page 2". A working offset returns mostly new
    # rows; an ignored one returns the same first page (≈0 new).
    threshold = max(1, page_size // 2)
    chosen_offset = ""
    chosen_is_page = False
    for cand in offset_candidates:
        try:
            text, rows = fetch_page({cand: str(page_size)})
        except (MercuryRequestError, MercuryNetworkError):
            continue
        new_here = sum(1 for r in rows if _row_signature(r) not in seen)
        if new_here >= threshold:
            chosen_offset = cand
            add_rows(rows)
            last_text = text
            if len(rows) < page_size:
                return all_rows, last_text, query
            break
    if not chosen_offset:
        for cand in page_candidates:
            try:
                text, rows = fetch_page({cand: "1"})  # 0-indexed page 1 == second page
            except (MercuryRequestError, MercuryNetworkError):
                continue
            new_here = sum(1 for r in rows if _row_signature(r) not in seen)
            if new_here >= threshold:
                chosen_offset = cand
                chosen_is_page = True
                add_rows(rows)
                last_text = text
                if len(rows) < page_size:
                    return all_rows, last_text, query
                break

    if not chosen_offset:
        # No pagination param worked — return the first page only.
        return all_rows, last_text, query

    # Continue from "page 3" onward.
    page_index = 2
    for _ in range(max_pages - 2):
        value = str(page_index) if chosen_is_page else str(page_size * page_index)
        text, rows = fetch_page({chosen_offset: value})
        added = add_rows(rows)
        last_text = text
        if added == 0 or len(rows) < page_size:
            break
        page_index += 1

    return all_rows, last_text, query


def list_available_queries(
    *, country: str | None = None, station: str | None = None
) -> list[dict[str, Any]]:
    return [q.to_dict() for q in list_queries(country=country, station=station)]


def probe_pagination(
    query_key: str,
    *,
    country: str | None = None,
    station: str | None = None,
    timeout: float = 25.0,
) -> list[dict[str, Any]]:
    """Diagnostic: fetch the query with several pagination params and
    report the row count of each, so we can identify which parameter
    Mercury honours to return more than the default page.
    """
    query = find_query(query_key, country=country, station=station)
    if query is None:
        raise LookupError(f"Mercury query '{query_key}' no definida.")

    session_cookies = _filter_cookies(_read_mercury_cookies())
    if not has_required(session_cookies, MERCURY_REQUIRED_COOKIES):
        raise MercuryAuthError(
            "No hay cookies de Mercury (mercury.amazon.com). Inicia sesion en el navegador."
        )

    env_verify = os.environ.get("MERCURY_VERIFY_TLS", "").strip().lower()
    verify = not (env_verify in ("0", "false", "no", "off"))
    headers = dict(DEFAULT_REQUEST_HEADERS)
    base_url = _request_url(query)
    qid = query.query_id
    region = (query.region or "eu").strip().lower() or "eu"

    def alt_path(path: str, extra: str = "") -> str:
        return f"https://{MERCURY_HOST}{path}?ID={qid}&region={region}{extra}"

    # Export/download variants — the dashboard "Download" usually hits an
    # export param or a sibling endpoint that returns the FULL result set
    # instead of the 1000-row page that getQueryResponse caps at.
    variants: list[tuple[str, str, dict[str, str]]] = [
        ("base", base_url, headers),
        ("format=csv", _append_params(base_url, {"format": "csv"}), headers),
        ("download=true", _append_params(base_url, {"download": "true"}), headers),
        ("downloadCsv=true", _append_params(base_url, {"downloadCsv": "true"}), headers),
        ("exportToCsv=true", _append_params(base_url, {"exportToCsv": "true"}), headers),
        ("export=csv", _append_params(base_url, {"export": "csv"}), headers),
        ("output=csv", _append_params(base_url, {"output": "csv"}), headers),
        ("responseType=csv", _append_params(base_url, {"responseType": "csv"}), headers),
        ("csv=true", _append_params(base_url, {"csv": "true"}), headers),
        ("contentType=csv", _append_params(base_url, {"contentType": "csv"}), headers),
        ("formatType=CSV", _append_params(base_url, {"formatType": "CSV"}), headers),
        ("path:/getQueryResponseCsv", alt_path("/getQueryResponseCsv"), headers),
        ("path:/downloadQueryResponse", alt_path("/downloadQueryResponse"), headers),
        ("path:/getQueryResponseAsCsv", alt_path("/getQueryResponseAsCsv"), headers),
        ("path:/exportQueryResponse", alt_path("/exportQueryResponse"), headers),
        ("path:/getQueryResultCsv", alt_path("/getQueryResultCsv"), headers),
        ("format=csv+download=true", _append_params(base_url, {"format": "csv", "download": "true"}), headers),
    ]

    out: list[dict[str, Any]] = []
    for label, url, hdrs in variants:
        try:
            text = _mercury_http_get(url, session_cookies, hdrs, timeout, verify)
            rows = parse_mercury_html(text, column_map=query.column_map)
            # CSV exports won't parse as an HTML table; report line count too.
            line_count = text.count("\n")
            out.append(
                {
                    "variant": label,
                    "url": url.split("?", 1)[-1][:80],
                    "row_count": len(rows),
                    "line_count": line_count,
                    "raw_length": len(text),
                    "looks_csv": ("<table" not in text.lower() and "," in text[:500]),
                }
            )
        except Exception as exc:  # noqa: BLE001
            out.append({"variant": label, "error": str(exc)[:200]})
    return out


__all__ = [
    "MERCURY_HOST",
    "MERCURY_LOGIN_URL",
    "MercuryAuthError",
    "MercuryNetworkError",
    "MercuryRequestError",
    "DEFAULT_REQUEST_HEADERS",
    "describe_request",
    "fetch_query",
    "has_mercury_cookies",
    "list_available_queries",
    "mercury_login_status",
    "parse_mercury_html",
    "parse_mercury_table",
    "fetch_html_table_by_id",
    "fetch_table_by_url",
    "fetch_table_for_query",
]
