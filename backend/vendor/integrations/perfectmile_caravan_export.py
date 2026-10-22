"""TEMPORARY shared Caravan-issued export acquisition layer.

Why this module exists
----------------------
Caravan's native dive-deep (``getDrilldownData`` / ``getBatchExportData``)
returns ZERO rows for several row-level metrics (their S3 partitions are empty
or lag by a day), even though the Caravan dashboard shows the aggregate count
and a working "Download" button. That button is driven by the GraphQL query
``getDiveDeepUrl``: Caravan returns a one-time, session-scoped CSV URL that
is consumed by the backend with the operator's authenticated PerfectMile
session. The CSV is built
from LPH dimension metadata (not from dive-deep partitions), which is exactly
why it works where the native rows path is empty.

This layer mirrors that button server-side for ANY metric:

1. Ask Caravan ``getDiveDeepUrl`` for the download URL (host is WHATEVER
   Caravan returns — never hardcoded here).
2. GET that URL invisibly with the operator's authenticated PerfectMile
   session.
3. Parse the CSV into row dicts with the dataset's own column headers.

Hard rules honored here:
  * The download URL is obtained ONLY from Caravan ``getDiveDeepUrl``; this
    module never constructs a ``perfectmile.amazon.com`` download URL, never
    touches ``bulk.json`` / ``bulk.csv`` literals or any legacy metric endpoint.
  * No fallback to a legacy transport, no synthetic values. Fail closed if
    Caravan returns no URL, the session is missing, or the CSV body is unusable.
  * All legacy-host contact is confined to THIS module so it can be deleted in
    one step once native Caravan dive-deep partitions exist. Removal path:
    delete this module and point each consumer back at a native Caravan rows
    profile.

History
-------
This generalizes the former DEX-only bridge
(``perfectmile_dex_divedeep.py``), which is now a backward-compatible shim that
re-exports these primitives under their DEX names so DEX imports stay valid.

Migration target (post 2026-10-15, legacy host sunset)
------------------------------------------------------
The authoritative permanent replacement is the Andes feedback/returns
contracts (do NOT implement here — isolate only). For DEX specifically the
target is ``andes."cdx-de".delivery_feedback_response``; for RTS/returns the
native ``or_rts_amzl_nd`` dive-deep partitions. When those land, replace
``fetch_export_rows`` with the native query and delete the getDiveDeepUrl
transport below.
"""
from __future__ import annotations

import base64
import csv
import io
import logging
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import requests

from app.integrations.perfectmile_caravan_client import (
    CaravanAuthError,
    gql,
)
from app.integrations.perfectmile_client import _is_auth_failure
from app.integrations.firefox_cookies import (
    MIDWAY_HOST_PATTERNS,
    _candidate_profile_dirs,
    read_cookie_records_from_profile,
    read_sessionstore_cookie_records,
)

LOGGER = logging.getLogger(__name__)

# Verbatim GraphQL operation that backs the dashboard "Download" button
# (PerfectMileCaravanFrontendUIAssets .../queries/diveDeepUrl.ts). The response
# carries only a URL string (+ optional errors), never rows.
_GET_DIVEDEEP_URL_QUERY = (
    "query getDiveDeepUrl($metricName: String!, $dimensionality: String!,"
    " $dateStart: String!, $dateEnd: String!, $hourStart: String, $hourEnd: String,"
    " $primaryRegion: Region!) {"
    " getDiveDeepUrl(metricName: $metricName, dimensionality: $dimensionality,"
    " dateStart: $dateStart, dateEnd: $dateEnd, hourStart: $hourStart,"
    " hourEnd: $hourEnd, primaryRegion: $primaryRegion) {"
    " url errors { fieldName errorMessage } } }"
)

_HTTP_TIMEOUT = 120.0
# A single day/station/arm is small; this guards against a pathological body.
_MAX_CSV_BYTES = 50 * 1024 * 1024
_FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# Per-metric candidate filename tokens used by the sanitized live probe.
# Tokens are normalized (lowercased, ``_``/``-`` -> space) before comparison.
EXPORT_METRIC_TOKENS: dict[str, tuple[str, ...]] = {
    "cust_feedb_neg_never_received_delivery": ("never received",),
    "cust_feedb_neg_delivered_to_wrong_address": ("wrong address",),
    "or_rts_amzl_nd": ("rts", "return to station", "amzl nd"),
    "adjusted_pnov_wankel": (
        "parcel not on van adjusted",
        "adjusted pnov",
        "pnov",
        "wankel",
    ),
    "ssh_lost_shipments": ("ssh", "lost shipments"),
    "fdds_ct_failure": ("ct failure", "central team", "fdds"),
    "dnr_concessions_only_amzl": (
        "delivered not received amzl",
        "dnr concessions",
        "delivered not received concessions",
    ),
    "dnr_w_delivered_with_failures": (
        "dnr concessions del with failures",
        "delivered with failures",
    ),
    "dnr_w_delivered_with_signals": (
        "dnr concessions del with signals",
        "delivered with signals",
    ),
    "scrublost": ("scrub lost", "scrublost"),
    "scrubmissing": ("scrub missing", "scrubmissing"),
    "dwc20_misses": ("dwc misses", "dwc20 misses", "dwc 2 0 misses"),
    "wubl60drivercount": ("wubl60 driver count", "wubl 60 driver count"),
    "ageing_shipments_3_days": ("ageing shipments 3 days", "aging shipments 3 days"),
    "ageing_shipments_4_days": ("ageing shipments 4 days", "aging shipments 4 days"),
    "ageing_shipments_5_days": ("ageing shipments 5 days", "aging shipments 5 days"),
    "ageing_shipments_6plus_days": (
        "ageing shipments 6plus days",
        "ageing shipments 6 plus days",
        "ageing shipments 6 days",
        "aging shipments 6plus days",
        "aging shipments 6 plus days",
        "aging shipments 6 days",
    ),
    # Observed verbatim in the getDiveDeepUrl response Content-Disposition.
    "missing_vsa_audits": ("missing vsa audits",),
}


class CaravanExportError(RuntimeError):
    """Base error for the shared Caravan-issued export layer."""


class CaravanExportAuthError(CaravanExportError):
    """The operator's PerfectMile/Midway session is missing or expired."""


class CaravanExportNoData(CaravanExportError):
    """Caravan issued a valid export, but it contains no data rows."""


class CaravanExportUnavailable(CaravanExportError):
    """Caravan returned no download URL for the metric (fail-closed, no fallback)."""


def _dimensionality_json(station: str, dimensionality: dict[str, Any] | None) -> str:
    import json as _json

    if dimensionality is None:
        dimensionality = {"location": [station]}
    return _json.dumps(dimensionality)


def get_export_url(
    metric_name: str,
    station: str,
    date_start: str,
    date_end: str | None = None,
    *,
    region: str = "EU",
    dimensionality: dict[str, Any] | None = None,
) -> str | None:
    """Return the Caravan-issued dive-deep download URL, or ``None``.

    ``None`` means Caravan reported ``errors`` or returned a falsy URL for the
    requested metric. A :class:`CaravanAuthError` propagates unchanged so the
    caller can map it to the typed ``auth_required`` outcome. This is the ONLY
    source of a temp export URL — never constructs a ``perfectmile.amazon.com``
    URL.
    """
    end = date_end or date_start
    data = gql(
        _GET_DIVEDEEP_URL_QUERY,
        {
            "metricName": metric_name,
            "dimensionality": _dimensionality_json(station, dimensionality),
            "dateStart": date_start,
            "dateEnd": end,
            "hourStart": None,
            "hourEnd": None,
            "primaryRegion": region,
        },
        "getDiveDeepUrl",
    )
    payload = data.get("getDiveDeepUrl") or {}
    if payload.get("errors"):
        LOGGER.warning(
            "getDiveDeepUrl returned errors for %s: %s",
            metric_name,
            str(payload.get("errors"))[:200],
        )
        return None
    url = str(payload.get("url") or "").strip()
    return url or None


_PM_PARTITION_HOSTS = (
    "perfectmile.a2z.com",
    ".perfectmile.a2z.com",
    "perfectmile.amazon.com",
    ".perfectmile.amazon.com",
)

# Authentication redirects are limited to the exact services used by the
# existing Caravan OIDC flow. The dynamically issued download host is allowed
# separately; no other Location target can receive cookies or Windows
# integrated authentication.
_AUTH_REDIRECT_HOSTS = frozenset(
    {
        "perfectmile.a2z.com",
        "midway-auth.amazon.com",
        "sentry.amazon.com",
        "idp.federate.amazon.com",
        "perfectmile-prod.auth.us-east-1.amazoncognito.com",
    }
)
_MAX_AUTH_REDIRECTS = 8
_MAX_NEGOTIATE_ROUNDS = 3


def _approved_auth_hosts(issued_host: str) -> frozenset[str]:
    """Return the exact hosts allowed in one Caravan-issued auth chain."""
    return _AUTH_REDIRECT_HOSTS | {issued_host}


def _validated_auth_url(url: str, issued_host: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise CaravanExportError("La autenticacion devolvio un destino no valido.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or host not in _approved_auth_hosts(issued_host)
    ):
        raise CaravanExportError("La autenticacion devolvio un destino no autorizado.")
    return url


def _download_sessions(url: str) -> Iterator[requests.Session]:
    """Yield ephemeral sessions for the exact host issued by Caravan.

    Persistent, session-store and Midway cookies stay bound to one Firefox
    profile. Domain/path/partition metadata is preserved in the temporary jar;
    no cookie or minted redirect state is persisted after the request.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme != "https" or not host:
        raise CaravanExportError("Caravan devolvio una URL de exportacion no valida.")

    for profile in _candidate_profile_dirs():
        if not isinstance(profile, Path) or not (profile / "cookies.sqlite").exists():
            continue
        try:
            persistent = read_cookie_records_from_profile(
                (host, f".{host}"),
                profile_dir=profile,
                target_host=host,
                partition_hosts=_PM_PARTITION_HOSTS,
            )
            session_cookies = read_sessionstore_cookie_records(
                host,
                profile_dir=profile,
                partition_hosts=_PM_PARTITION_HOSTS,
            )
            midway = read_cookie_records_from_profile(
                MIDWAY_HOST_PATTERNS,
                profile_dir=profile,
                target_host="midway-auth.amazon.com",
            )
        except (OSError, RuntimeError):
            continue
        cookie_records = [*session_cookies, *persistent, *midway]
        if not cookie_records:
            continue

        session = requests.Session()
        session.headers.update(
            {
                "Accept": "text/csv,application/csv,text/plain,*/*",
                "User-Agent": _FIREFOX_UA,
            }
        )
        for cookie in reversed(cookie_records):
            domain = cookie.host.strip() or host
            session.cookies.set(
                cookie.name,
                cookie.value,
                domain=domain,
                path=cookie.path or "/",
                secure=cookie.secure,
            )
        yield session


def _challenge_token(header: str) -> bytes | None:
    match = re.search(
        r"(?:^|,)\s*Negotiate(?:\s+([A-Za-z0-9+/=]+))?",
        str(header or ""),
        re.IGNORECASE,
    )
    if not match or not match.group(1):
        return None
    try:
        return base64.b64decode(match.group(1))
    except (TypeError, ValueError):
        return None


def _retry_negotiate(
    session: requests.Session,
    response: requests.Response,
    *,
    issued_host: str,
) -> requests.Response:
    """Answer a Negotiate challenge only for an approved HTTPS host."""
    if response.status_code != 401 or "negotiate" not in str(
        response.headers.get("WWW-Authenticate") or ""
    ).casefold():
        return response
    challenge_url = _validated_auth_url(response.request.url, issued_host)
    challenge_host = (urlparse(challenge_url).hostname or "").strip().lower()
    if challenge_host not in _approved_auth_hosts(issued_host):
        raise CaravanExportAuthError(
            "El host de autenticacion no esta autorizado para Negotiate."
        )
    try:
        import spnego

        context = spnego.client(
            hostname=challenge_host,
            service="HTTP",
            protocol="negotiate",
        )
    except Exception:  # noqa: BLE001 - unavailable OS auth stays typed auth
        return response

    current = response
    incoming = _challenge_token(current.headers.get("WWW-Authenticate", ""))
    for _ in range(_MAX_NEGOTIATE_ROUNDS):
        try:
            outgoing = context.step(incoming)
        except Exception:  # noqa: BLE001 - invalid local credential context
            return current
        if not outgoing:
            return current
        request = current.request.copy()
        _validated_auth_url(request.url, issued_host)
        request.headers["Authorization"] = (
            "Negotiate " + base64.b64encode(outgoing).decode("ascii")
        )
        current.close()
        try:
            current = session.send(
                request,
                timeout=_HTTP_TIMEOUT,
                allow_redirects=False,
                verify=True,
            )
        except requests.RequestException:
            raise
        if current.status_code != 401:
            return current
        incoming = _challenge_token(current.headers.get("WWW-Authenticate", ""))
    return current


def _get_with_auth_redirects(
    session: requests.Session, url: str
) -> requests.Response:
    """Follow a bounded set of approved HTTPS redirects and challenges."""
    issued_host = (urlparse(url).hostname or "").strip().lower()
    current_url = _validated_auth_url(url, issued_host)
    for redirect_count in range(_MAX_AUTH_REDIRECTS + 1):
        response = session.get(
            current_url,
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
            verify=True,
        )
        response = _retry_negotiate(session, response, issued_host=issued_host)
        location = str(response.headers.get("Location") or "")
        if response.status_code in {301, 302, 303, 307, 308} and location:
            if redirect_count >= _MAX_AUTH_REDIRECTS:
                response.close()
                raise CaravanExportError(
                    "La autenticacion del export supero el limite de redirects."
                )
            target = urljoin(response.url, location)
            try:
                current_url = _validated_auth_url(target, issued_host)
            except CaravanExportError:
                response.close()
                raise
            response.close()
            continue
        return response
    raise CaravanExportError("La autenticacion del export supero el limite de redirects.")


def fetch_export_content(url: str) -> tuple[bytes, str]:
    """Fetch one Caravan-issued export invisibly and return body plus filename header."""
    attempted = False
    auth_failed = False
    network_error: requests.RequestException | None = None
    for session in _download_sessions(url):
        attempted = True
        response: requests.Response | None = None
        try:
            response = _get_with_auth_redirects(session, url)

            if _is_auth_failure(response):
                # The first redirect walk can mint transient cookies but finish
                # on an auth page. Retry the exact issued URL once inside the
                # same ephemeral jar before classifying the profile as blocked.
                response.close()
                response = _get_with_auth_redirects(session, url)
            if _is_auth_failure(response):
                auth_failed = True
                continue
            if response.status_code != 200:
                raise CaravanExportError(f"El CSV devolvio HTTP {response.status_code}.")

            content_type = str(response.headers.get("Content-Type") or "")
            media_type = content_type.partition(";")[0].strip().casefold()
            if (
                media_type in {"text/html", "application/json", "text/json", "application/xml", "text/xml"}
                or media_type.endswith("+json")
                or media_type.endswith("+xml")
            ):
                raise CaravanExportError(
                    f"La descarga devolvio un tipo de contenido no CSV ({media_type})."
                )
            body = response.content or b""
            if len(body) > _MAX_CSV_BYTES:
                raise CaravanExportError("El CSV supera el tamano maximo admitido.")
            return body, str(response.headers.get("Content-Disposition") or "")
        except requests.RequestException as exc:
            network_error = exc
            continue
        finally:
            if response is not None:
                response.close()
            session.close()

    if not attempted or auth_failed:
        raise CaravanExportAuthError(
            "No hay una sesion autenticada disponible para el export de PerfectMile."
        )
    if network_error is not None:
        raise CaravanExportError("Error de red al recuperar el CSV.") from network_error
    raise CaravanExportAuthError(
        "No hay una sesion autenticada disponible para el export de PerfectMile."
    )


def fetch_export_rows(
    metric_name: str,
    station: str,
    date_start: str,
    date_end: str | None = None,
    *,
    region: str = "EU",
    dimensionality: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch a Caravan-issued export CSV and return row dicts (server fast path).

    Fail-closed:
      * no URL from Caravan -> :class:`CaravanExportUnavailable`.
      * no PerfectMile cookies / HTTP auth failure -> :class:`CaravanExportAuthError`.
      * valid export without data rows -> :class:`CaravanExportNoData`.
      * HTTP error, oversized or malformed body -> :class:`CaravanExportError`.
    """
    url = get_export_url(
        metric_name, station, date_start, date_end, region=region, dimensionality=dimensionality
    )
    if not url:
        raise CaravanExportUnavailable(
            f"Caravan returned no dive-deep URL for {metric_name}."
        )

    body, _filename = fetch_export_content(url)
    return _parse_csv_text(body.decode("utf-8-sig", errors="replace"))


# ---------------------------------------------------------------------------
# Shared CSV parsing for invisible backend acquisition and safe probes
# ---------------------------------------------------------------------------


def _normalize_csv_header(value: object) -> str:
    """Return a conservative snake-case key for one export header."""
    text = str(value or "").lstrip("\ufeff").strip().casefold()
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text)).strip("_")


def normalize_export_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize row keys without changing or synthesizing cell values.

    Conflicting headers that collapse to the same canonical key are malformed;
    accepting either value would make schema validation depend on column order.
    """
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized: dict[str, Any] = {}
        for raw_key, value in row.items():
            key = _normalize_csv_header(raw_key)
            if not key:
                continue
            if key in normalized and normalized[key] != value:
                raise CaravanExportError(
                    f"El CSV contiene cabeceras duplicadas para '{key}'."
                )
            normalized[key] = value
        normalized_rows.append(normalized)
    return normalized_rows


def _obvious_error_body(stripped: str) -> bool:
    """Return whether a compact response is clearly an error, not CSV."""
    lead = stripped[:4096].strip()
    folded = lead.casefold()
    if re.search(
        r"<(?:!doctype\s+html|html|head|body|title|div|span|pre|script|\?xml)\b",
        folded,
    ):
        return True
    if lead.startswith(("{", "[", '"')):
        try:
            import json as _json

            if isinstance(_json.loads(lead), (dict, list, str)):
                return True
        except (TypeError, ValueError):
            pass

    first_line = lead.splitlines()[0].strip()
    try:
        first_fields = next(csv.reader([first_line], strict=True), [])
    except csv.Error:
        return True
    normalized_fields = {_normalize_csv_header(field) for field in first_fields}
    if normalized_fields & _SINGLE_COLUMN_EXPORT_HEADERS:
        return False

    # Normalize compact status tokens (``AccessDenied``, ``RequestFailed``)
    # as well as punctuation/spacing before applying broad transport-error
    # semantics. Unknown one-line text still fails the header plausibility
    # gate below, so this does not rely on an exhaustive error-message regex.
    de_camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", first_line)
    words = re.sub(r"[^a-z0-9]+", " ", de_camel.casefold()).strip()
    tokens = set(words.split())
    status_tokens = {
        "denied", "error", "exception", "expired", "failed", "failure",
        "forbidden", "invalid", "rejected", "timeout", "unauthorized",
        "unavailable",
    }
    status_phrases = (
        "access denied", "bad gateway", "could not", "gateway timeout",
        "internal server", "not found", "request failed", "service unavailable",
        "timed out", "too many requests", "unable to", "went wrong",
    )
    return bool(tokens & status_tokens) or any(phrase in words for phrase in status_phrases)


# A one-column body is indistinguishable from arbitrary plain text unless its
# header is one of the row identifiers actually used by the shared consumers.
# Header-only multi-column exports must carry at least one of these identifiers
# too; this distinguishes a real empty tabular contract from comma-separated
# error prose without guessing every possible upstream message.
_SINGLE_COLUMN_EXPORT_HEADERS = frozenset(
    {
        "tracking_id",
        "scannable_id",
        "shipment_id",
        "package_id",
        "barcode",
        "transporter_id",
        "assigned_route_id",
        "route_id",
        "dsp",
        "vin",
        "vrn",
        "vrns",
    }
)
_HEADER_TEXT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _./%()\-]*$")


def _plausible_header_only(raw_headers: list[str], headers: list[str]) -> bool:
    """Validate the schema signal required to classify an empty export."""
    return bool(set(headers) & _SINGLE_COLUMN_EXPORT_HEADERS) and all(
        _HEADER_TEXT_RE.fullmatch(str(raw).strip()) for raw in raw_headers
    )


def _parse_csv_text(text: str) -> list[dict[str, Any]]:
    """Validate and parse CSV text into normalized row dictionaries.

    A blank body or a plausible header-only export is a valid no-data
    response. JSON, HTML, plain-text errors and malformed CSV are transport
    failures. This parser is shared by every invisible backend acquisition.
    """
    stripped = text.lstrip("\ufeff\r\n\t ")
    if not stripped:
        raise CaravanExportNoData("El CSV no contiene filas.")
    if _obvious_error_body(stripped):
        raise CaravanExportError("La descarga no devolvio un CSV.")
    try:
        reader = csv.reader(io.StringIO(text), strict=True)
        raw_headers = next(reader, None)
        if raw_headers is None:
            raise CaravanExportNoData("El CSV no contiene filas.")
        if (
            not raw_headers
            or len(raw_headers) > 512
            or any("\x00" in str(header) or len(str(header)) > 256 for header in raw_headers)
        ):
            raise CaravanExportError("El CSV no tiene cabecera valida.")
        headers = [_normalize_csv_header(header) for header in raw_headers]
        if any(not header for header in headers):
            raise CaravanExportError("El CSV no tiene cabecera valida.")
        if len(headers) != len(set(headers)):
            raise CaravanExportError("El CSV contiene cabeceras duplicadas.")
        if len(headers) == 1 and headers[0] not in _SINGLE_COLUMN_EXPORT_HEADERS:
            raise CaravanExportError("La descarga no devolvio un CSV reconocible.")

        rows: list[dict[str, Any]] = []
        for values in reader:
            if not any(str(value or "").strip() for value in values):
                continue
            if len(values) > len(headers):
                raise CaravanExportError("El CSV contiene filas con columnas extra.")
            padded = values + [""] * (len(headers) - len(values))
            rows.append(dict(zip(headers, padded)))
    except CaravanExportError:
        raise
    except csv.Error as exc:
        raise CaravanExportError(f"El CSV no se pudo leer: {exc}") from exc
    if not rows:
        if _plausible_header_only(raw_headers, headers):
            raise CaravanExportNoData("El CSV no contiene filas.")
        raise CaravanExportError("La descarga no devolvio un CSV reconocible.")
    return rows


def parse_csv_bytes(data: bytes) -> list[dict[str, Any]]:
    """Parse an in-memory CSV body (manual upload path). Fail-closed."""
    if data is None:
        raise CaravanExportError("El CSV llego vacio.")
    if len(data) > _MAX_CSV_BYTES:
        raise CaravanExportError("El CSV supera el tamano maximo admitido.")
    return _parse_csv_text(bytes(data).decode("utf-8-sig", errors="replace"))


__all__ = [
    "CaravanAuthError",
    "CaravanExportAuthError",
    "CaravanExportError",
    "CaravanExportNoData",
    "CaravanExportUnavailable",
    "EXPORT_METRIC_TOKENS",
    "fetch_export_content",
    "fetch_export_rows",
    "get_export_url",
    "normalize_export_rows",
    "parse_csv_bytes",
    "_parse_csv_text",
]
