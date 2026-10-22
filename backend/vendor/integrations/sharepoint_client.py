"""Read and write SharePoint Online files directly over the REST API.

This lets the app update the HUB Delivery RTS workbook **online** —
without needing the file synced locally through OneDrive — by reusing
the SharePoint session cookies stored in the user's Firefox profile
(same philosophy as the Mercury / QuickSight integrations).

Authentication
--------------
SharePoint Online sets the ``FedAuth`` (and ``rtFa``) session cookies
host-only on the tenant host (e.g. ``amazoneur.sharepoint.com``) once the
operator has logged in through the corporate federation. Those cookies
are enough to call the SharePoint REST API.

Write flow
----------
1. Parse the configured SharePoint URL into ``(host, web, server-relative
   path, folder, filename)``.
2. ``GET .../_api/web/GetFileByServerRelativeUrl('<path>')/$value`` to
   download the current workbook bytes.
3. The caller edits the bytes (openpyxl in memory).
4. ``POST .../_api/contextinfo`` to obtain the form digest.
5. ``POST .../_api/web/GetFolderByServerRelativeUrl('<folder>')/Files/add
   (url='<filename>',overwrite=true)`` with the digest header and the new
   bytes as the body — this overwrites the file in place (a new version).

The ``sourcedoc`` GUID URL form (the one Excel Online shows in the address
bar, ``/_layouts/15/Doc.aspx?sourcedoc={GUID}``) is supported too: the
server-relative path is resolved first with ``GetFileById``.

Every public function raises :class:`SharePointError` with a clear,
user-facing message on failure; callers translate that into a response.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests

from app.integrations.firefox_cookies import (
    SHAREPOINT_REQUIRED_COOKIES,
    SharePointCookieCandidate,
    has_required,
    sharepoint_cookie_candidates,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 60
_VERBOSE = {"Accept": "application/json;odata=verbose"}

# Upload retry policy: 409 is SharePoint's "save conflict" (two machines
# overwriting the same file in the same instant — routine with several
# operators autosaving the same shared workbook) and 5xx are transient
# server errors. Both typically clear within seconds. Total worst-case
# extra wait: 1.5s + 3s = 4.5s before the error surfaces to the operator.
_UPLOAD_ATTEMPTS = 3
_UPLOAD_BACKOFF_S = 1.5
_RETRYABLE_UPLOAD_STATUSES = frozenset({409, 500, 502, 503, 504})
# Matches the SharePoint "preview/sharing" link prefixes, e.g. ``/:x:/r/``,
# ``/:w:/s/``, ``/:b:/g/`` … which sit in front of the real path.
_PREVIEW_PREFIX = re.compile(r"^/:[a-zA-Z]:/[a-zA-Z]/")
_GUID = re.compile(r"[0-9a-fA-F-]{36}")

# Only the non-secret SHA-256 identity of a proven cookie pair is remembered.
# Persist it outside the Firefox profile so normal backend reloads keep the
# browser-proven ordering; raw cookie values never enter this state or logs.
_MAX_READ_CANDIDATE_ATTEMPTS = 8
_READ_CANDIDATE_RETRY_BUDGET_S = 20.0
_sharepoint_candidate_lock = threading.Lock()
_preferred_candidate_by_host: dict[str, str] = {}
_preferred_candidates_loaded = False


def _preferred_candidate_path() -> Path:
    configured = (os.environ.get("TOOLBOX_RUNTIME_STATE_DIR") or "").strip()
    base = Path(configured) if configured else Path(__file__).resolve().parents[2] / "data"
    return base / "sharepoint_cookie_candidate.json"


def _load_preferred_candidates() -> None:
    global _preferred_candidates_loaded
    with _sharepoint_candidate_lock:
        if _preferred_candidates_loaded:
            return
        _preferred_candidates_loaded = True
        try:
            data = json.loads(_preferred_candidate_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        for host, fingerprint in data.items():
            host_key = str(host or "").strip().lower()
            identity = str(fingerprint or "").strip().lower()
            if host_key and re.fullmatch(r"[0-9a-f]{64}", identity):
                _preferred_candidate_by_host[host_key] = identity


def _save_preferred_candidates_locked() -> None:
    path = _preferred_candidate_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(
            json.dumps(_preferred_candidate_by_host, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        logger.warning("No se pudo guardar la preferencia de sesión SharePoint", exc_info=True)


class SharePointError(Exception):
    """Raised on any SharePoint REST failure with a user-facing message.

    ``reason`` is a short machine-readable code the route can map to a
    localized message (``auth`` | ``parse`` | ``download`` | ``upload`` |
    ``digest`` | ``not_found`` | ``conflict``).
    """

    def __init__(self, message: str, *, reason: str = "error") -> None:
        super().__init__(message)
        self.reason = reason


def _network_error(exc: requests.RequestException, *, reason: str) -> SharePointError:
    """Translate a transport failure (DNS, VPN, timeout…) into a short,
    user-facing error.

    The raw ``requests`` exception (``HTTPSConnectionPool… Max retries
    exceeded… NameResolutionError…``) is useless and scary for the
    operator, so it only goes to the log; the UI just gets a retry hint.
    """
    logger.warning("SharePoint sin conexión (%s): %s", reason, exc)
    return SharePointError(
        "No conectado a SharePoint. Comprueba la conexión (VPN/red) y "
        "reinténtalo.",
        reason="network",
    )


@dataclass
class SharePointTarget:
    host: str
    web: str  # server-relative web path, e.g. "/sites/HubDeliveryES"
    server_relative: str  # full path to the file
    folder: str  # server-relative folder containing the file
    filename: str
    sourcedoc: str = ""  # GUID when the URL is the Doc.aspx form
    share_link: str = ""  # full URL when it is an encoded share link (/:x:/s/…/<token>)

    @property
    def base(self) -> str:
        return f"https://{self.host}{self.web}"


def is_sharepoint_url(value: str) -> bool:
    v = (value or "").strip().lower()
    return v.startswith(("http://", "https://")) and ".sharepoint.com" in v


# Hosts the tenant federation redirects an *unauthenticated* request to.
# A SharePoint REST call without valid ``FedAuth`` cookies answers with a
# 302 to one of these (which ``requests`` silently follows), landing on a
# sign-in HTML page with HTTP 200 — NOT a 401/403. We must treat that as
# an auth failure instead of a success.
_LOGIN_HOST_HINTS = (
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.windows.net",
    "login.live.com",
    "adfs",
    "sts.",
    "fs.",
)


def _response_is_login_redirect(resp: "requests.Response") -> bool:
    """True when ``resp`` is the federation sign-in page, not the REST API.

    Detects two unauthenticated patterns:

    * the request was redirected to a Microsoft/ADFS login host, or
    * the body came back as HTML (the REST API answers JSON, and ``$value``
      answers a binary stream — an HTML body means a sign-in page).
    """
    try:
        final_host = urlsplit(resp.url or "").netloc.lower()
    except Exception:  # noqa: BLE001 - never raise from a probe
        final_host = ""
    if final_host and any(hint in final_host for hint in _LOGIN_HOST_HINTS):
        return True
    # Any redirect hop that pointed at a login host also counts.
    for hop in getattr(resp, "history", None) or []:
        loc = (hop.headers.get("Location") or "").lower()
        if any(hint in loc for hint in _LOGIN_HOST_HINTS):
            return True
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        return True
    return False


def parse_sharepoint_url(url: str) -> SharePointTarget:
    """Parse a SharePoint web/sharing URL into its addressable parts."""
    url = url.strip()
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if not host or ".sharepoint.com" not in host:
        raise SharePointError(
            "La URL configurada no es de SharePoint Online.", reason="parse"
        )

    had_preview_prefix = bool(_PREVIEW_PREFIX.match(parts.path))
    path = _PREVIEW_PREFIX.sub("/", parts.path)
    path = unquote(path)

    # New-style "Copiar vínculo" links encode the file as an opaque share
    # token instead of a real path, e.g.
    # ``/:x:/s/HubDeliveryES/IQBQteEqdZSkSIFjxf8yBAKAAWuHNRgJ4NvsOLlkYLbs0NM``.
    # After stripping the preview prefix the last segment has no file
    # extension — a real path form (``/:x:/r/sites/.../file.xlsx``) always
    # ends in a filename. Those links can only be resolved against the
    # server (see ``_resolve_share_link``), so flag them here.
    if had_preview_prefix:
        last = path.rsplit("/", 1)[-1]
        if last and "." not in last:
            return SharePointTarget(
                host=host, web="", server_relative="", folder="",
                filename="", share_link=url,
            )

    # Derive the web (site) path: /sites/<name> or /teams/<name>; otherwise
    # everything before /_layouts/, or the host root.
    segs = [s for s in path.split("/") if s]
    web = ""
    if len(segs) >= 2 and segs[0].lower() in ("sites", "teams"):
        web = "/" + "/".join(segs[:2])
    elif "/_layouts/" in path.lower():
        web = path[: path.lower().index("/_layouts/")]

    # Doc.aspx?sourcedoc={GUID} form (file open in Excel Online).
    sourcedoc = ""
    if "/_layouts/" in path.lower():
        query = parse_qs(parts.query)
        raw = (query.get("sourcedoc") or query.get("SourceDoc") or [""])[0]
        match = _GUID.search(raw or "")
        if match:
            sourcedoc = match.group(0)
        if not sourcedoc:
            raise SharePointError(
                "No se pudo identificar el archivo en la URL de SharePoint. "
                "Usa el enlace 'Copiar vínculo' del archivo .xlsx.",
                reason="parse",
            )
        return SharePointTarget(host=host, web=web, server_relative="",
                                folder="", filename="", sourcedoc=sourcedoc)

    if not path or path == "/":
        raise SharePointError(
            "La URL de SharePoint no apunta a un archivo. Usa el enlace "
            "'Copiar vínculo' del archivo .xlsx.",
            reason="parse",
        )

    server_relative = path
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    filename = path.rsplit("/", 1)[-1]
    if not web:
        web = ""  # call _api against the host root

    # Los enlaces "Copiar vínculo" con ruta llevan además el GUID del
    # documento en ``?d=w<GUID>``. Se guarda como fallback: si la ruta del
    # enlace se queda obsoleta (archivo movido o renombrado en SharePoint,
    # el enlace sigue funcionando en el navegador pero la ruta ya no
    # existe), ``download_file`` reintenta resolviendo por GUID.
    doc_guid = ""
    query = parse_qs(parts.query)
    raw_d = (query.get("d") or query.get("D") or [""])[0]
    match = _GUID.search(raw_d or "")
    if match:
        doc_guid = match.group(0)

    return SharePointTarget(
        host=host, web=web, server_relative=server_relative,
        folder=folder, filename=filename, sourcedoc=doc_guid,
    )


def _ordered_sharepoint_candidates(
    host: str,
    *,
    exclude: set[str] | None = None,
) -> list[SharePointCookieCandidate]:
    _load_preferred_candidates()
    candidates = sharepoint_cookie_candidates(host)
    excluded = exclude or set()
    candidates = [item for item in candidates if item.fingerprint not in excluded]
    with _sharepoint_candidate_lock:
        preferred = _preferred_candidate_by_host.get(host)
    candidates.sort(
        key=lambda item: (item.fingerprint == preferred, item.rank),
        reverse=True,
    )
    return candidates


def _remember_sharepoint_candidate(host: str, fingerprint: str) -> None:
    _load_preferred_candidates()
    with _sharepoint_candidate_lock:
        if _preferred_candidate_by_host.get(host) == fingerprint:
            return
        _preferred_candidate_by_host[host] = fingerprint
        _save_preferred_candidates_locked()


def _reject_sharepoint_candidate(host: str, fingerprint: str) -> None:
    _load_preferred_candidates()
    with _sharepoint_candidate_lock:
        if _preferred_candidate_by_host.get(host) == fingerprint:
            _preferred_candidate_by_host.pop(host, None)
            _save_preferred_candidates_locked()


class _RotatingSharePointSession(requests.Session):
    """Session that tries bounded coherent candidates after read auth rejects."""

    def __init__(self, host: str, candidate: SharePointCookieCandidate) -> None:
        super().__init__()
        self._sharepoint_host = host
        self._sharepoint_fingerprint = ""
        self._apply_candidate(candidate)
        self.headers.update({"User-Agent": "ToolBox/1.0"})

    def _apply_candidate(self, candidate: SharePointCookieCandidate) -> None:
        self.cookies.clear()
        self.cookies.update(candidate.cookies)
        self._sharepoint_fingerprint = candidate.fingerprint

    def _switch_candidate(self, attempted: set[str]) -> bool:
        alternatives = _ordered_sharepoint_candidates(
            self._sharepoint_host,
            exclude=attempted,
        )
        if not alternatives:
            return False
        self._apply_candidate(alternatives[0])
        return True

    @staticmethod
    def _auth_rejected(response: requests.Response) -> bool:
        return response.status_code == 401 or _response_is_login_redirect(response)

    @staticmethod
    def _bounded_retry_kwargs(kwargs: dict, remaining: float) -> dict:
        bounded = dict(kwargs)
        timeout = bounded.get("timeout")
        if isinstance(timeout, (int, float)):
            bounded["timeout"] = max(0.1, min(float(timeout), remaining))
        else:
            bounded["timeout"] = max(0.1, min(float(_TIMEOUT), remaining))
        return bounded

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        can_retry = method.upper() in {"GET", "HEAD", "OPTIONS"}
        attempted: set[str] = set()
        deadline = time.monotonic() + _READ_CANDIDATE_RETRY_BUDGET_S
        response = super().request(method, url, **kwargs)

        while self._auth_rejected(response):
            rejected = self._sharepoint_fingerprint
            attempted.add(rejected)
            _reject_sharepoint_candidate(self._sharepoint_host, rejected)
            if (
                not can_retry
                or len(attempted) >= _MAX_READ_CANDIDATE_ATTEMPTS
                or time.monotonic() >= deadline
                or not self._switch_candidate(attempted)
            ):
                return response
            response.close()
            remaining = deadline - time.monotonic()
            response = super().request(
                method,
                url,
                **self._bounded_retry_kwargs(kwargs, remaining),
            )

        _remember_sharepoint_candidate(
            self._sharepoint_host,
            self._sharepoint_fingerprint,
        )
        return response


def _session(host: str) -> requests.Session:
    candidates = _ordered_sharepoint_candidates(host)
    if not candidates or not has_required(
        candidates[0].cookies,
        SHAREPOINT_REQUIRED_COOKIES,
    ):
        raise SharePointError(
            "No hay sesión de SharePoint en el navegador. Abre el archivo RTS "
            "en SharePoint con Firefox (inicia sesión) y vuelve a intentarlo.",
            reason="auth",
        )
    return _RotatingSharePointSession(host, candidates[0])


def _target_from_server_relative(host: str, server_relative: str) -> SharePointTarget:
    """Build a fully-resolved target from a host + server-relative file path."""
    segs = [s for s in server_relative.split("/") if s]
    web = ""
    if len(segs) >= 2 and segs[0].lower() in ("sites", "teams"):
        web = "/" + "/".join(segs[:2])
    folder = server_relative.rsplit("/", 1)[0] if "/" in server_relative else ""
    filename = server_relative.rsplit("/", 1)[-1]
    return SharePointTarget(
        host=host, web=web, server_relative=server_relative,
        folder=folder, filename=filename,
    )


def _resolve_share_link(sess: requests.Session, target: SharePointTarget) -> SharePointTarget:
    """Resolve an encoded share link (``/:x:/s/<site>/<token>``) to its file.

    Requesting the share link with ``download=1`` makes SharePoint answer a
    302 whose ``Location`` is the real server-relative path of the file —
    without downloading anything. That path is what the REST API needs.
    """
    url = target.share_link
    sep = "&" if "?" in url else "?"
    try:
        resp = sess.get(f"{url}{sep}download=1", timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="download")

    location = resp.headers.get("Location") or ""
    if resp.status_code in (301, 302, 303, 307, 308) and location:
        low = location.lower()
        if any(hint in low for hint in _LOGIN_HOST_HINTS):
            raise SharePointError(
                "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
                "iniciar sesión en SharePoint con Firefox y reconecta.",
                reason="auth",
            )
        loc_parts = urlsplit(location)
        loc_path = unquote(loc_parts.path)

        # ``download=1`` no siempre redirige a la ruta real del archivo:
        # SharePoint suele contestar con ``/_layouts/15/download.aspx`` y el
        # documento identificado en la query (``UniqueId`` con el GUID, o
        # ``SourceUrl`` con la ruta). Tratar ese path como si fuera el
        # archivo (acaba en ".aspx", así que "tiene extensión") producía un
        # 404 en el GetFileByServerRelativeUrl posterior.
        if "/_layouts/" in loc_path.lower():
            query = parse_qs(loc_parts.query)

            def _q(*names: str) -> str:
                for name in names:
                    for key, values in query.items():
                        if key.lower() == name.lower() and values:
                            return values[0]
                return ""

            source_url = unquote(_q("SourceUrl"))
            if source_url:
                source_path = urlsplit(source_url).path or source_url
                if source_path and "." in source_path.rsplit("/", 1)[-1]:
                    return _target_from_server_relative(target.host, unquote(source_path))
            match = _GUID.search(_q("UniqueId", "sourcedoc", "docid") or "")
            if match:
                web = loc_path[: loc_path.lower().index("/_layouts/")]
                probe = SharePointTarget(
                    host=target.host, web=web, server_relative="",
                    folder="", filename="", sourcedoc=match.group(0),
                )
                return _resolve_sourcedoc(sess, probe)
        elif loc_path and "." in loc_path.rsplit("/", 1)[-1]:
            return _target_from_server_relative(target.host, loc_path)

    if resp.status_code in (401, 403):
        raise SharePointError(
            "SharePoint rechazó la sesión (no autorizado). Vuelve a iniciar "
            "sesión en el archivo RTS con Firefox.",
            reason="auth",
        )
    if resp.status_code == 404:
        raise SharePointError("No se encontró el archivo RTS en SharePoint.", reason="not_found")
    raise SharePointError(
        "No se pudo resolver el enlace compartido de SharePoint. Comprueba "
        "que el enlace 'Copiar vínculo' apunta al archivo .xlsx y que tienes "
        "sesión iniciada en SharePoint con Firefox.",
        reason="parse",
    )


def _resolve_sourcedoc(sess: requests.Session, target: SharePointTarget) -> SharePointTarget:
    """Resolve the server-relative path for a Doc.aspx?sourcedoc GUID URL."""
    url = f"{target.base}/_api/web/GetFileById('{target.sourcedoc}')"
    try:
        resp = sess.get(url, headers=_VERBOSE, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="download")
    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
            "iniciar sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code in (401, 403):
        raise SharePointError(
            "SharePoint rechazó la sesión (no autorizado). Vuelve a iniciar "
            "sesión en el archivo RTS con Firefox.",
            reason="auth",
        )
    if resp.status_code == 404:
        raise SharePointError("No se encontró el archivo RTS en SharePoint.", reason="not_found")
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al localizar el archivo.",
            reason="download",
        )
    try:
        info = resp.json()["d"]
        server_relative = info["ServerRelativeUrl"]
    except (ValueError, KeyError) as exc:
        raise SharePointError(f"Respuesta inesperada de SharePoint: {exc}", reason="download")
    folder = server_relative.rsplit("/", 1)[0] if "/" in server_relative else ""
    filename = server_relative.rsplit("/", 1)[-1]
    return SharePointTarget(
        host=target.host, web=target.web, server_relative=server_relative,
        folder=folder, filename=filename, sourcedoc=target.sourcedoc,
    )


def download_file(url: str) -> tuple[bytes, SharePointTarget, requests.Session]:
    """Download the SharePoint file bytes.

    Returns ``(content, resolved_target, session)`` so the caller can
    reuse the authenticated session for the subsequent upload.
    """
    target = parse_sharepoint_url(url)
    sess = _session(target.host)
    if target.share_link:
        target = _resolve_share_link(sess, target)
    elif target.sourcedoc and not target.server_relative:
        target = _resolve_sourcedoc(sess, target)

    def _fetch(t: SharePointTarget) -> requests.Response:
        api = f"{t.base}/_api/web/GetFileByServerRelativeUrl('{t.server_relative}')/$value"
        try:
            return sess.get(api, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            raise _network_error(exc, reason="download")

    resp = _fetch(target)

    # Ruta obsoleta (archivo movido/renombrado): el enlace con ruta lleva
    # también el GUID del documento (``?d=w<GUID>``) — resolver por GUID y
    # reintentar antes de rendirse.
    if resp.status_code == 404 and target.sourcedoc:
        target = _resolve_sourcedoc(sess, target)
        resp = _fetch(target)

    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
            "iniciar sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code in (401, 403):
        raise SharePointError(
            "SharePoint rechazó la sesión (no autorizado). Vuelve a iniciar "
            "sesión en el archivo RTS con Firefox.",
            reason="auth",
        )
    if resp.status_code == 404:
        raise SharePointError(
            "No se encontró el archivo RTS en SharePoint (el enlace apunta a "
            "una ruta que ya no existe). Copia de nuevo el enlace del archivo "
            "con 'Copiar vínculo' y actualízalo en Configuración.",
            reason="not_found",
        )
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al descargar el archivo.",
            reason="download",
        )
    return resp.content, target, sess


def _form_digest(sess: requests.Session, target: SharePointTarget) -> str:
    url = f"{target.base}/_api/contextinfo"
    try:
        resp = sess.post(url, headers=_VERBOSE, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="digest")
    if resp.status_code in (401, 403):
        raise SharePointError(
            "SharePoint rechazó la sesión al obtener el token. Vuelve a "
            "iniciar sesión con Firefox.",
            reason="auth",
        )
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al obtener el token.",
            reason="digest",
        )
    try:
        return resp.json()["d"]["GetContextWebInformation"]["FormDigestValue"]
    except (ValueError, KeyError) as exc:
        raise SharePointError(f"Token de SharePoint no válido: {exc}", reason="digest")


def upload_file(target: SharePointTarget, content: bytes, sess: requests.Session) -> None:
    """Overwrite the file in place with ``content`` (creates a new version)."""
    digest = _form_digest(sess, target)
    api = (
        f"{target.base}/_api/web/GetFolderByServerRelativeUrl('{target.folder}')"
        f"/Files/add(url='{target.filename}',overwrite=true)"
    )
    headers = {
        "X-RequestDigest": digest,
        "Content-Type": "application/octet-stream",
    }
    try:
        resp = sess.post(api, headers=headers, data=content, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="upload")
    # A 200 that is really the federation sign-in page (silently expired
    # session) must NOT count as a successful upload — the file was never
    # written on SharePoint.
    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión al guardar el RTS (sesión "
            "caducada). Vuelve a iniciar sesión en SharePoint con Firefox y "
            "reconecta.",
            reason="auth",
        )
    if resp.status_code == 401:
        raise SharePointError(
            "SharePoint rechazó la sesión al guardar el RTS. Vuelve a iniciar "
            "sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code == 403:
        raise SharePointError(
            "No tienes permiso de edición sobre el archivo RTS en SharePoint. "
            "Solicita acceso de edición al propietario del sitio.",
            reason="forbidden",
        )
    if resp.status_code == 423:
        raise SharePointError(
            "El archivo RTS está bloqueado en SharePoint (otro usuario lo tiene "
            "abierto en Excel). Contacta con el POC e inténtalo de nuevo.",
            reason="locked",
        )
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al guardar el archivo.",
            reason="upload",
        )


# ---------------------------------------------------------------------------
# Path-based operations (folders, download/upload by server-relative path)
# ---------------------------------------------------------------------------
#
# The functions above operate on a single configured file URL (the HUB RTS
# use case). The Toolbox storage layer instead needs to address arbitrary
# folders/files under a site by their *server-relative path* and to create
# the folder tree on demand (``<STATION>/Config`` and
# ``<STATION>/<YYYY>/<MM. Mes>``). The helpers below add that capability,
# reusing the same Firefox-cookie session and form-digest machinery.


def site_base(host: str, web: str) -> str:
    """Return the REST base URL for a site web, e.g.
    ``https://amazoneur.sharepoint.com/sites/Toolbox``.
    """
    host = (host or "").strip().lower()
    web = (web or "").strip()
    if web and not web.startswith("/"):
        web = "/" + web
    return f"https://{host}{web}".rstrip("/")


def build_session(host: str) -> requests.Session:
    """Public wrapper around the cookie-authenticated session factory."""
    return _session(host)


def _quote_sr(path: str) -> str:
    """URL-encode a server-relative path while keeping the ``/`` separators."""
    return quote(path or "", safe="/")


def _digest_for_base(sess: requests.Session, base: str) -> str:
    """Obtain a form digest for ``base`` (site web REST root)."""
    url = f"{base}/_api/contextinfo"
    try:
        resp = sess.post(url, headers=_VERBOSE, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="digest")
    if resp.status_code in (401, 403):
        raise SharePointError(
            "SharePoint rechazó la sesión al obtener el token. Vuelve a "
            "iniciar sesión con Firefox.",
            reason="auth",
        )
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al obtener el token.",
            reason="digest",
        )
    try:
        return resp.json()["d"]["GetContextWebInformation"]["FormDigestValue"]
    except (ValueError, KeyError) as exc:
        raise SharePointError(f"Token de SharePoint no válido: {exc}", reason="digest")


def folder_exists(sess: requests.Session, base: str, server_relative_folder: str) -> bool:
    """Return True if the folder exists, False on 404."""
    url = f"{base}/_api/web/GetFolderByServerRelativeUrl('{_quote_sr(server_relative_folder)}')"
    try:
        resp = sess.get(url, headers=_VERBOSE, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="download")
    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
            "iniciar sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    if resp.status_code == 401:
        raise SharePointError(
            "SharePoint rechazó la sesión (no autorizado). Inicia sesión en "
            "SharePoint con Firefox y vuelve a intentarlo.",
            reason="auth",
        )
    if resp.status_code == 403:
        raise SharePointError(
            "No tienes acceso a esta carpeta en SharePoint. Solicita acceso al "
            "propietario del sitio.",
            reason="forbidden",
        )
    raise SharePointError(
        f"SharePoint devolvió {resp.status_code} al comprobar la carpeta.",
        reason="download",
    )


def _create_folder(sess: requests.Session, base: str, digest: str, server_relative_folder: str) -> None:
    """Create a single folder (its parent must already exist)."""
    url = f"{base}/_api/web/folders/add('{_quote_sr(server_relative_folder)}')"
    headers = {"X-RequestDigest": digest, **_VERBOSE}
    try:
        resp = sess.post(url, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="upload")
    if resp.ok:
        return
    # Conflict / already exists: tolerate it if the folder is now present.
    if folder_exists(sess, base, server_relative_folder):
        return
    if resp.status_code == 401:
        raise SharePointError(
            "SharePoint rechazó la sesión al crear la carpeta. Vuelve a iniciar "
            "sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code == 403:
        raise SharePointError(
            "No tienes permiso de edición en SharePoint para crear carpetas. "
            "Solicita acceso de edición al propietario del sitio.",
            reason="forbidden",
        )
    raise SharePointError(
        f"SharePoint devolvió {resp.status_code} al crear la carpeta "
        f"'{server_relative_folder}'.",
        reason="upload",
    )


def ensure_folder_path(
    sess: requests.Session,
    base: str,
    web: str,
    server_relative_folder: str,
    digest: str,
) -> None:
    """Create every missing folder segment between ``web`` and the target.

    ``server_relative_folder`` must live under ``web`` (the site path).
    Each level is created idempotently; existing folders are skipped.
    """
    web = (web or "").rstrip("/")
    target = (server_relative_folder or "").rstrip("/")
    if not target:
        return
    if web and target.startswith(web):
        rel = target[len(web):].strip("/")
    else:
        rel = target.strip("/")
    segments = [s for s in rel.split("/") if s]
    current = web
    for seg in segments:
        current = f"{current}/{seg}"
        if not folder_exists(sess, base, current):
            _create_folder(sess, base, digest, current)


def download_path(
    sess: requests.Session, base: str, server_relative_path: str
) -> bytes | None:
    """Download file bytes by server-relative path. Returns None on 404."""
    api = (
        f"{base}/_api/web/GetFileByServerRelativeUrl"
        f"('{_quote_sr(server_relative_path)}')/$value"
    )
    try:
        resp = sess.get(api, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="download")
    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
            "iniciar sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code == 404:
        return None
    if resp.status_code == 401:
        raise SharePointError(
            "SharePoint rechazó la sesión (no autorizado). Inicia sesión en "
            "SharePoint con Firefox y vuelve a intentarlo.",
            reason="auth",
        )
    if resp.status_code == 403:
        raise SharePointError(
            "No tienes acceso a este archivo en SharePoint. Solicita acceso al "
            "propietario del sitio.",
            reason="forbidden",
        )
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al descargar el archivo.",
            reason="download",
        )
    return resp.content


def file_exists(sess: requests.Session, base: str, server_relative_path: str) -> bool:
    """Return True if the file exists at the server-relative path."""
    api = f"{base}/_api/web/GetFileByServerRelativeUrl('{_quote_sr(server_relative_path)}')"
    try:
        resp = sess.get(api, headers=_VERBOSE, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="download")
    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
            "iniciar sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    if resp.status_code == 401:
        raise SharePointError(
            "SharePoint rechazó la sesión (no autorizado). Inicia sesión en "
            "SharePoint con Firefox y vuelve a intentarlo.",
            reason="auth",
        )
    if resp.status_code == 403:
        raise SharePointError(
            "No tienes acceso a este archivo en SharePoint. Solicita acceso al "
            "propietario del sitio.",
            reason="forbidden",
        )
    raise SharePointError(
        f"SharePoint devolvió {resp.status_code} al comprobar el archivo.",
        reason="download",
    )


def upload_path(
    sess: requests.Session,
    base: str,
    web: str,
    server_relative_folder: str,
    filename: str,
    content: bytes,
    *,
    digest: str | None = None,
    overwrite: bool = True,
) -> None:
    """Upload ``content`` to ``<folder>/<filename>`` creating folders as needed.

    Existing callers keep the historical overwrite behavior. With
    ``overwrite=False`` SharePoint performs a create-only write: this helper
    makes a single attempt and reports name collisions as ``reason=conflict``
    so the owning feature can reconcile an ambiguous response against the
    exact bytes it tried to publish before deciding whether to retry.
    """
    if digest is None:
        digest = _digest_for_base(sess, base)
    ensure_folder_path(sess, base, web, server_relative_folder, digest)
    overwrite_token = "true" if overwrite else "false"
    api = (
        f"{base}/_api/web/GetFolderByServerRelativeUrl('{_quote_sr(server_relative_folder)}')"
        f"/Files/add(url='{quote(filename)}',overwrite={overwrite_token})"
    )

    # Overwrite saves retain the shared-workbook retry policy. Create-only
    # writes never retry here: a timeout/5xx may mean the file was committed,
    # and blindly posting again would turn one logical save into two versions.
    attempts = _UPLOAD_ATTEMPTS if overwrite else 1
    last_status = 0
    for attempt in range(attempts):
        if attempt:
            time.sleep(_UPLOAD_BACKOFF_S * attempt)
            try:
                digest = _digest_for_base(sess, base)
            except SharePointError:
                pass  # keep the previous digest; the retry may still succeed
        headers = {
            "X-RequestDigest": digest,
            "Content-Type": "application/octet-stream",
        }
        try:
            resp = sess.post(api, headers=headers, data=content, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            if attempt < attempts - 1:
                logger.warning("SharePoint upload: network error (retry %s): %s", attempt + 1, exc)
                continue
            raise _network_error(exc, reason="upload")
        # A 200 that is really the federation sign-in page (silently expired
        # session) must NOT count as a successful upload — the file was never
        # written on SharePoint.
        if _response_is_login_redirect(resp):
            raise SharePointError(
                "SharePoint pidió iniciar sesión al guardar (sesión caducada). "
                "Vuelve a iniciar sesión en SharePoint con Firefox y reconecta.",
                reason="auth",
            )
        if resp.status_code == 401:
            raise SharePointError(
                "SharePoint rechazó la sesión al guardar. Vuelve a iniciar sesión "
                "en SharePoint con Firefox y reconecta.",
                reason="auth",
            )
        if resp.status_code == 403:
            raise SharePointError(
                "No tienes permiso de edición en SharePoint. Solicita acceso de "
                "edición al propietario del sitio.",
                reason="forbidden",
            )
        if resp.status_code == 423:
            raise SharePointError(
                "El archivo está bloqueado en SharePoint (otro usuario lo tiene "
                "abierto en Excel). Contacta con el POC e inténtalo de nuevo.",
                reason="locked",
            )
        if resp.ok:
            return
        if not overwrite:
            conflict_text = str(getattr(resp, "text", "") or "").lower()
            if resp.status_code == 400:
                try:
                    conflict_text += " " + json.dumps(resp.json()).lower()
                except (TypeError, ValueError, AttributeError):
                    pass
            is_existing_file = (
                "already exists" in conflict_text
                or "file exists" in conflict_text
                or ("microsoft.sharepoint.spexception" in conflict_text and "exist" in conflict_text)
            )
            if resp.status_code == 409 or (resp.status_code == 400 and is_existing_file):
                raise SharePointError(
                    f"El archivo '{filename}' ya existe en SharePoint.",
                    reason="conflict",
                )
        last_status = resp.status_code
        if resp.status_code in _RETRYABLE_UPLOAD_STATUSES and attempt < attempts - 1:
            logger.warning(
                "SharePoint upload: %s en '%s' (reintento %s/%s)",
                resp.status_code,
                filename,
                attempt + 1,
                attempts - 1,
            )
            continue
        break
    raise SharePointError(
        f"SharePoint devolvió {last_status} al guardar el archivo.",
        reason="upload",
    )


def list_folder(
    sess: requests.Session, base: str, server_relative_folder: str
) -> list[dict[str, object]]:
    """List the files directly inside ``server_relative_folder``.

    Returns one entry per file with ``{name, server_relative, size,
    time_last_modified}``. Empty list when the folder does not exist or
    has no files. Subfolders are NOT recursed.
    """
    api = (
        f"{base}/_api/web/GetFolderByServerRelativeUrl"
        f"('{_quote_sr(server_relative_folder)}')/Files"
        "?$select=Name,ServerRelativeUrl,Length,TimeLastModified"
    )
    try:
        resp = sess.get(api, headers=_VERBOSE, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="download")
    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
            "iniciar sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code == 404:
        return []
    if resp.status_code == 401:
        raise SharePointError(
            "SharePoint rechazó la sesión (no autorizado). Inicia sesión en "
            "SharePoint con Firefox y vuelve a intentarlo.",
            reason="auth",
        )
    if resp.status_code == 403:
        raise SharePointError(
            "No tienes acceso a esta carpeta en SharePoint.",
            reason="forbidden",
        )
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al listar la carpeta.",
            reason="download",
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise SharePointError(
            f"Respuesta inesperada de SharePoint: {exc}", reason="download"
        )
    # ``odata=verbose`` wraps the array under d.results; the lean format
    # returns ``value`` directly. Handle both.
    items = []
    if isinstance(body, dict):
        d = body.get("d")
        if isinstance(d, dict):
            results = d.get("results")
            if isinstance(results, list):
                items = results
        if not items and isinstance(body.get("value"), list):
            items = body["value"]
    out: list[dict[str, object]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name") or entry.get("name") or ""
        server_rel = entry.get("ServerRelativeUrl") or entry.get("serverRelativeUrl") or ""
        if not name or not server_rel:
            continue
        out.append(
            {
                "name": str(name),
                "server_relative": str(server_rel),
                "size": int(entry.get("Length") or entry.get("length") or 0),
                "time_last_modified": str(
                    entry.get("TimeLastModified")
                    or entry.get("timeLastModified")
                    or ""
                ),
            }
        )
    return out


def recycle_path(
    sess: requests.Session,
    base: str,
    server_relative_path: str,
    *,
    digest: str | None = None,
) -> bool:
    """Move a file to the SharePoint recycle bin. Returns True on success.

    Recycling is the reversible alternative to a hard delete; the file
    can be restored from the site recycle bin within the tenant
    retention window (90 days by default). Returns False when the file
    does not exist; raises ``SharePointError`` on other failures.
    """
    if digest is None:
        digest = _digest_for_base(sess, base)
    api = (
        f"{base}/_api/web/GetFileByServerRelativeUrl"
        f"('{_quote_sr(server_relative_path)}')/recycle"
    )
    headers = {"X-RequestDigest": digest, **_VERBOSE}
    try:
        resp = sess.post(api, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise _network_error(exc, reason="delete")
    if resp.status_code == 404:
        return False
    if _response_is_login_redirect(resp):
        raise SharePointError(
            "SharePoint pidió iniciar sesión (sesión caducada). Vuelve a "
            "iniciar sesión en SharePoint con Firefox y reconecta.",
            reason="auth",
        )
    if resp.status_code == 401:
        raise SharePointError(
            "SharePoint rechazó la sesión al eliminar.",
            reason="auth",
        )
    if resp.status_code == 403:
        raise SharePointError(
            "No tienes permiso de edición en SharePoint para eliminar archivos.",
            reason="forbidden",
        )
    if not resp.ok:
        raise SharePointError(
            f"SharePoint devolvió {resp.status_code} al eliminar el archivo.",
            reason="delete",
        )
    return True
