"""Read cookies from the user's local Firefox profile without opening a browser.

Firefox stores cookies in ``cookies.sqlite`` inside each profile folder.
We locate the default profile via ``profiles.ini``, copy the file to a
temporary location (Firefox keeps an exclusive lock while running) and
read the cookies that match the host patterns we care about.

Public API:

* :func:`find_firefox_profile_dir` — locate the default profile folder.
* :func:`read_cookies_from_profile` — return ``{cookie_name: value}`` for
  all cookies whose host matches any of the provided patterns.
* :func:`harmony_cookies_from_profile` / :func:`pod_cookies_from_profile`
  — convenience wrappers that apply the DINO host patterns.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote


def _now_seconds() -> float:
    return time.time()


HARMONY_API_HOST = "customer-calling-details.eu-west-1.api.harmony.a2z.com"
HARMONY_HOST_PATTERNS = (
    HARMONY_API_HOST,
    "console.harmony.a2z.com",
    ".console.harmony.a2z.com",
    ".harmony.a2z.com",
)

POD_HOST_PATTERNS = (
    "honeypot.amazon.dev",
    ".honeypot.amazon.dev",
)

PERFECTMILE_HOST_PATTERNS = (
    "perfectmile.amazon.com",
    ".perfectmile.amazon.com",
)

MERCURY_HOST_PATTERNS = (
    "mercury.amazon.com",
    ".mercury.amazon.com",
    ".amazon.com",
)

SIPHON_HOST_PATTERNS = (
    "siphon-monolith.corp.amazon.com",
    ".siphon-monolith.corp.amazon.com",
    "siphon.amazon.dev",
    ".siphon.amazon.dev",
    ".amazon.dev",
    ".amazon.com",
)

# Metis (metis.step.amazon.dev) — the DO Forecast +3Wks plan portal.
# The SPA on ``metis.step.amazon.dev`` calls its API on
# ``api.metis.step.amazon.dev`` authenticated via Midway, same as the
# rest of the Amazon-internal integrations. The network trace shows just
# ``amzn_sso_token`` + ``amzn_sso_rfp`` on the request, so we read those
# from the user's Firefox profile.
METIS_HOST_PATTERNS = (
    "api.metis.step.amazon.dev",
    ".metis.step.amazon.dev",
    "metis.step.amazon.dev",
    ".step.amazon.dev",
    ".amazon.dev",
)

# routingtools.amazon.com — used by the Plan Manifest auto-fetch
# endpoint (downloadManifest?stationCode=...&sessionName=CYCLE_X&planDate=...).
# The portal authenticates via Midway/Federate session cookies, same as
# the rest of Amazon-internal sites. We read them from the user's
# Firefox profile to avoid asking the operator for a bearer token.
ROUTINGTOOLS_HOST_PATTERNS = (
    "routingtools.amazon.com",
    ".routingtools.amazon.com",
    ".amazon.com",
)

# Amazon Relay tracker. The SPA lives on ``track.relay.amazon.dev`` and
# its API requires a Midway-minted Bearer id_token with audience
# ``track.relay.amazon.dev``. We mint that token against Midway using the
# midway-auth session cookies below (same Firefox session as the other
# integrations) — see :mod:`app.integrations.relay_client`.
RELAY_HOST_PATTERNS = (
    "track.relay.amazon.dev",
    ".track.relay.amazon.dev",
    "cloudfront.eu.api.relay.amazon.dev",
    ".relay.amazon.dev",
    ".amazon.dev",
)

# logistics.amazon.es (Cortex) — the Flow Meeting / Cierre auto-fetch of
# the "Itinerarios de Cortex" data. The portal authenticates via the
# Midway/Federate ``logistics-federate-token`` JWT (plus the usual
# ``session-id`` / ``session-token`` pair). We read them from the user's
# Firefox profile — see :mod:`app.integrations.cortex_client`.
LOGISTICS_HOST_PATTERNS = (
    "logistics.amazon.es",
    ".logistics.amazon.es",
    "logistics.amazon.com",
    ".logistics.amazon.com",
    # SCC ("boson") station dashboards live on the .co.uk host for the EU
    # region, even for ES stations (DCZ4). The proxy API gateway
    # (``/station/proxyapigateway/data``) is served from here — see
    # :mod:`app.integrations.scc_client`.
    "logistics.amazon.co.uk",
    ".logistics.amazon.co.uk",
    ".amazon.co.uk",
    ".amazon.es",
    ".amazon.com",
)
LOGISTICS_REQUIRED_COOKIES = ("logistics-federate-token",)

# Midway SSO host: the ``/SSO`` endpoint mints OIDC id_tokens for a given
# ``client_id`` (audience) when called with these session cookies.
MIDWAY_HOST_PATTERNS = (
    "midway-auth.amazon.com",
    ".midway-auth.amazon.com",
    ".amazon.com",
    "amazon.com",
)
MIDWAY_REQUIRED_COOKIES = ("session",)

# Amazon QuickSight. The dashboard SPA lives on
# ``<region>.quicksight.aws.amazon.com`` and authenticates through the
# AWS console federation (Midway/Federate -> signin.aws.amazon.com).
# QuickSight then sets its own session cookies on ``.aws.amazon.com`` /
# the regional host. We read whatever is there and forward it; the exact
# auth cookie name is environment-specific, hence no strict "required"
# set — :mod:`app.integrations.quicksight_client` validates the call by
# its HTTP result instead.
QUICKSIGHT_HOST_PATTERNS = (
    "us-east-1.quicksight.aws.amazon.com",
    ".quicksight.aws.amazon.com",
    "quicksight.aws.amazon.com",
    "signin.aws.amazon.com",
    ".aws.amazon.com",
    "aws.amazon.com",
)
QUICKSIGHT_REQUIRED_COOKIES: tuple[str, ...] = ()

# Microsoft SharePoint Online. The web session is authenticated through
# the tenant federation (``login.microsoftonline.com`` ->
# ``<tenant>.sharepoint.com``) which sets the ``FedAuth`` (and ``rtFa``)
# session cookies host-only on the tenant SharePoint host. These cookies
# are enough to call the SharePoint REST API (download/upload a file) —
# see :mod:`app.integrations.sharepoint_client`. The tenant host varies
# per organisation, so the convenience wrapper builds the patterns from
# the host parsed out of the configured RTS URL.
# Route Diver / RouteIQ (eu.eagle.routeiq.last-mile.amazon.dev). The
# ``cachedRouteDiveResult`` endpoint authenticates via Midway, so the
# SSO JWT (``amzn_sso_token`` + ``amzn_sso_rfp``) is what we read from
# Firefox. The token is issued under ``.amazon.dev`` — see
# :mod:`app.integrations.route_diver_client`.
ROUTEDIVER_HOST_PATTERNS = (
    "eu.eagle.routeiq.last-mile.amazon.dev",
    ".eagle.routeiq.last-mile.amazon.dev",
    ".routeiq.last-mile.amazon.dev",
    ".last-mile.amazon.dev",
    ".amazon.dev",
)

SHAREPOINT_HOST_PATTERNS = (
    ".sharepoint.com",
    "sharepoint.com",
)
SHAREPOINT_REQUIRED_COOKIES = ("FedAuth",)

# FCLM portal (``fclm-portal.amazon.com``) — PPA / Process Path Rollup pages.
# Midway-authenticated: the portal session cookie is set host-only after the
# ``/sso/login`` -> midway redirect. Amazon-wide cookies (``.amazon.com``)
# ride along. See :mod:`app.integrations.fclm_precision_client`.
FCLM_HOST_PATTERNS = (
    "fclm-portal.amazon.com",
    ".fclm-portal.amazon.com",
    ".amazon.com",
    "amazon.com",
)
FCLM_REQUIRED_COOKIES: tuple[str, ...] = ()

# Horizon (Labor Planning) — the SPA lives on ``horizon.harmony.a2z.com`` but
# its data API is fronted by ``horizon.eu-west-1.api.harmony.a2z.com``. That
# host authenticates via Midway (``amzn_sso_token`` + ``amzn_sso_rfp`` set
# host-only by the ``/sso/login`` -> midway ``?id_token=`` redirect). Those
# cookies authorize ``POST /v1/sso/assume`` which mints the temporary AWS
# credentials the SPA then uses to SigV4-sign the ``execute-api`` calls
# (``*.execute-api.us-west-2.amazonaws.com/prod/{baselines,primaries,...}``).
# See :mod:`app.integrations.horizon_client`.
HORIZON_API_HOST = "horizon.eu-west-1.api.harmony.a2z.com"
HORIZON_HOST_PATTERNS = (
    HORIZON_API_HOST,
    ".eu-west-1.api.harmony.a2z.com",
    ".api.harmony.a2z.com",
    ".harmony.a2z.com",
)
HORIZON_REQUIRED_COOKIES = ("amzn_sso_token",)

# CloudWatch internal dashboards (``cw-dashboards.aka.amazon.com``). The
# console authenticates via Midway (``amzn_sso_token`` + ``aws-userInfo`` +
# the retail ``session-id``/``session-token`` pair). Those cookies authorize
# the console's own ``auth.V4BatchSign`` endpoint, which mints the SigV4
# headers the browser uses to call ``monitoring.<region>.amazonaws.com``
# (GetMetricData). See :mod:`app.integrations.cw_dashboards_client`.
CWDASH_HOST = "cw-dashboards.aka.amazon.com"
CWDASH_HOST_PATTERNS = (
    CWDASH_HOST,
    ".aka.amazon.com",
    ".amazon.com",
    "amazon.com",
)
CWDASH_REQUIRED_COOKIES = ("amzn_sso_token",)

HARMONY_REQUIRED_COOKIES = ("amzn_sso_token",)
POD_REQUIRED_COOKIES = ("amzn_sso_token", "amzn_sso_rfp")
PERFECTMILE_REQUIRED_COOKIES = ("amzn_sso_token",)
# Mercury now authenticates with the host-scoped Midway token. The retail
# session-id/session-token pair may still be forwarded when Firefox has it,
# but its absence must not keep the current Mercury host permanently red.
MERCURY_REQUIRED_COOKIES = ("amzn_sso_token",)
ROUTINGTOOLS_REQUIRED_COOKIES = ("amzn_sso_token",)
RELAY_REQUIRED_COOKIES = ("amzn_sso_token",)
# Siphon authenticates via Midway. The server-side ``user`` / ``realm`` /
# ``domain`` / ``enable_midway`` cookies the network inspector shows are
# session cookies that the backend SETS in the response — Firefox doesn't
# persist them to ``cookies.sqlite`` so we cannot read them. Once the
# Midway JWT is valid the server re-issues those session cookies on the
# first call, so requiring just ``amzn_sso_token`` is enough.
SIPHON_REQUIRED_COOKIES = ("amzn_sso_token",)
# Metis authenticates via Midway too — ``amzn_sso_token`` is the JWT we
# can read from Firefox; ``amzn_sso_rfp`` rides along when present.
METIS_REQUIRED_COOKIES = ("amzn_sso_token",)

# Route Diver / RouteIQ authenticates via Midway as well. The browser
# sends ``amzn_sso_token`` (the JWT) plus ``amzn_sso_rfp``; the token is
# enough to ride the warm-up redirect that mints the session cookies.
ROUTEDIVER_REQUIRED_COOKIES = ("amzn_sso_token",)

def _firefox_data_root() -> Path | None:
    env_override = os.getenv("FIREFOX_PROFILE_DIR", "").strip()
    if env_override:
        path = Path(env_override).expanduser()
        if path.exists():
            return path

    if sys.platform.startswith("win"):
        appdata = os.getenv("APPDATA", "")
        if appdata:
            candidate = Path(appdata) / "Mozilla" / "Firefox"
            if candidate.exists():
                return candidate
    elif sys.platform == "darwin":
        candidate = Path.home() / "Library" / "Application Support" / "Firefox"
        if candidate.exists():
            return candidate
    else:
        for rel in (".mozilla/firefox", "snap/firefox/common/.mozilla/firefox"):
            candidate = Path.home() / rel
            if candidate.exists():
                return candidate
    return None


def _read_profiles_ini(firefox_root: Path) -> list[Path]:
    ini_path = firefox_root / "profiles.ini"
    if not ini_path.exists():
        return []

    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(ini_path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        try:
            parser.read(ini_path, encoding="latin-1")
        except configparser.Error:
            return []

    def _path_key(value: str) -> str:
        return value.replace("\\", "/").strip("/").casefold()

    install_defaults: dict[str, bool] = {}
    for section in parser.sections():
        if not section.lower().startswith("install"):
            continue
        default = parser.get(section, "Default", fallback="").strip()
        if not default:
            continue
        locked = parser.get(section, "Locked", fallback="0").strip() == "1"
        key = _path_key(default)
        install_defaults[key] = install_defaults.get(key, False) or locked

    profiles: list[tuple[int, str, Path]] = []
    for section in parser.sections():
        if not section.lower().startswith("profile"):
            continue
        path = parser.get(section, "Path", fallback="").strip()
        if not path:
            continue
        is_relative = parser.get(section, "IsRelative", fallback="1").strip() == "1"
        resolved = (firefox_root / path) if is_relative else Path(path)
        if not resolved.exists():
            continue
        is_default = parser.get(section, "Default", fallback="0").strip() == "1"
        install_key = _path_key(path)
        if install_defaults.get(install_key):
            priority = 4
        elif install_key in install_defaults:
            priority = 3
        elif is_default:
            priority = 2
        else:
            priority = 1
        profiles.append((priority, path.casefold(), resolved))

    profiles.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in profiles]


def _candidate_profile_dirs() -> list[Path]:
    firefox_root = _firefox_data_root()
    if firefox_root is None:
        return []

    # ``FIREFOX_PROFILE_DIR`` historically accepts either the Firefox data
    # root or one concrete profile. Preserve both forms: a concrete profile
    # is authoritative and must not be reinterpreted as a root after a PC or
    # profile migration.
    if (firefox_root / "cookies.sqlite").exists():
        return [firefox_root]

    ordered = _read_profiles_ini(firefox_root)
    if ordered:
        return ordered

    profiles_dir = firefox_root / "Profiles"
    if not profiles_dir.exists():
        return []
    return [item for item in profiles_dir.iterdir() if item.is_dir()]


def find_firefox_profile_dir(cookie_filename: str = "cookies.sqlite") -> Path | None:
    """Return the first profile directory that contains ``cookie_filename``."""
    for candidate in _candidate_profile_dirs():
        if (candidate / cookie_filename).exists():
            return candidate
    return None


def _host_matches(host: str, patterns: Iterable[str]) -> bool:
    host = (host or "").lower()
    if not host:
        return False
    for raw in patterns:
        pattern = (raw or "").lower()
        if not pattern:
            continue
        if pattern.startswith("."):
            suffix = pattern
            if host == suffix.lstrip(".") or host.endswith(suffix):
                return True
        elif host == pattern:
            return True
    return False


def _copy_cookie_db(profile_dir: Path) -> Path:
    """Copy ``cookies.sqlite`` (and WAL/SHM if present) to a temp dir."""
    src = profile_dir / "cookies.sqlite"
    if not src.exists():
        raise FileNotFoundError(f"No se encontro cookies.sqlite en {profile_dir}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="firefox_cookies_"))
    dst = tmp_dir / "cookies.sqlite"
    shutil.copy2(src, dst)
    for sidecar in ("cookies.sqlite-wal", "cookies.sqlite-shm"):
        sidecar_path = profile_dir / sidecar
        if sidecar_path.exists():
            try:
                shutil.copy2(sidecar_path, tmp_dir / sidecar)
            except OSError:
                pass
    return dst


def _host_specificity_score(host: str, target_host: str | None) -> int:
    """Higher score = closer to ``target_host`` per cookie scoping rules.

    Used to break ties when several cookies share a name across
    different hosts (e.g. the central Amazon SSO ``amzn_sso_token``
    is set on dozens of hosts; only the one bound to ``target_host``
    or one of its parent domains is valid for that target).

    Returns:
      * 1000 — host-only cookie matching exactly the target host
      * 900..100 — domain cookie ``.parent`` of the target (closer
        parents score higher)
      * 0 — cookie that does not actually apply to ``target_host``
    """
    if not target_host:
        # No target = pure latest-wins, score everyone equally.
        return 1
    host = (host or "").lower().lstrip(".")
    target = target_host.lower().lstrip(".")
    if not host:
        return 0
    # Exact host-only.
    if host == target:
        return 1000
    # Domain cookie applies if target ends with ``.host``.
    if target.endswith("." + host):
        # Closer parents (longer host) score higher.
        return 100 + len(host)
    return 0


@dataclass(frozen=True)
class FirefoxCookie:
    """One Firefox cookie with its browser scoping metadata intact."""

    name: str
    value: str
    host: str
    path: str
    expiry: int
    last_accessed: int
    origin_attributes: str
    secure: bool


@dataclass(frozen=True)
class SharePointCookieCandidate:
    """One coherent SharePoint auth pair from one Firefox profile/source."""

    cookies: dict[str, str]
    fingerprint: str
    profile: Path
    source: str
    origin_attributes: str
    rank: tuple[int, int, int, str]


def _partition_host(origin_attributes: str) -> str:
    """Extract Firefox's top-level partition host without exposing values."""
    decoded = unquote(str(origin_attributes or ""))
    match = re.search(r"(?:^|\^)partitionKey=\([^,]+,([^)]+)\)", decoded)
    if not match:
        match = re.search(r"(?:^|\^)partitionKey=(?:https?://)?([^\^/]+)", decoded)
    return (match.group(1) if match else "").strip().lower().lstrip(".")


def read_cookie_records_from_profile(
    host_patterns: Iterable[str],
    profile_dir: Path | None = None,
    *,
    include_expired: bool = False,
    target_host: str | None = None,
    partition_hosts: Iterable[str] = (),
) -> list[FirefoxCookie]:
    """Return matching cookies while preserving domain, path and partition.

    Direct cookies must apply to ``target_host`` when supplied. Partitioned
    cookies are additionally retained when their top-level site matches one of
    ``partition_hosts``; their original domain/path remains intact so a
    temporary HTTP jar only sends them on the appropriate redirect hop.
    """
    profile = profile_dir or find_firefox_profile_dir()
    if profile is None:
        raise RuntimeError(
            "No se encontro el perfil de Firefox. Define FIREFOX_PROFILE_DIR o instala Firefox."
        )
    db_path = profile / "cookies.sqlite"
    if not db_path.exists():
        raise RuntimeError(f"No se encontro cookies.sqlite en {profile}")

    connection = None
    tmp_db = None
    try:
        tmp_db = _copy_cookie_db(profile)
        connection = sqlite3.connect(str(tmp_db))
    except (sqlite3.OperationalError, OSError):
        if tmp_db:
            shutil.rmtree(tmp_db.parent, ignore_errors=True)
            tmp_db = None
        try:
            connection = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"No se pudo abrir cookies.sqlite: {exc}") from exc

    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(moz_cookies)")
        }
        origin_expr = "originAttributes" if "originAttributes" in columns else "''"
        secure_expr = "isSecure" if "isSecure" in columns else "1"
        cursor = connection.execute(
            "SELECT host, path, name, value, lastAccessed, expiry, "
            f"{origin_expr}, {secure_expr} FROM moz_cookies"
        )
        now_unix = int(_now_seconds())
        partition_patterns = tuple(partition_hosts)
        records: list[FirefoxCookie] = []
        for host, path, name, value, last_accessed, expiry, origin_attrs, secure in cursor.fetchall():
            if not name or value is None:
                continue
            exp = int(expiry or 0)
            if not include_expired and 0 < exp <= now_unix:
                continue
            direct = _host_matches(str(host or ""), host_patterns)
            if direct and target_host and _host_specificity_score(str(host or ""), target_host) <= 0:
                direct = False
            partitioned = bool(
                partition_patterns
                and _host_matches(_partition_host(str(origin_attrs or "")), partition_patterns)
            )
            if not direct and not partitioned:
                continue
            records.append(
                FirefoxCookie(
                    name=str(name),
                    value=str(value),
                    host=str(host or ""),
                    path=str(path or "/"),
                    expiry=exp,
                    last_accessed=int(last_accessed or 0),
                    origin_attributes=str(origin_attrs or ""),
                    secure=bool(secure),
                )
            )
        records.sort(key=lambda item: item.last_accessed, reverse=True)
        return records
    finally:
        connection.close()
        if tmp_db:
            shutil.rmtree(tmp_db.parent, ignore_errors=True)


def read_cookies_from_profile(
    host_patterns: Iterable[str],
    profile_dir: Path | None = None,
    *,
    include_expired: bool = False,
    target_host: str | None = None,
) -> dict[str, str]:
    """Return cookies whose host matches any pattern. Latest wins on conflicts.

    By default expired cookies are skipped (``expiry <= now()``).

    When ``target_host`` is provided we honour browser-style cookie
    scoping: each candidate is scored against ``target_host`` and on
    ties the highest score wins (host-only match beats parent-domain
    match beats anything else). This avoids picking up cookies named
    the same on unrelated hosts (e.g. ``amzn_sso_token`` set on a
    backend service while we want the one issued for the public
    site we are calling).
    """
    profile = profile_dir or find_firefox_profile_dir()
    if profile is None:
        raise RuntimeError(
            "No se encontro el perfil de Firefox. Define FIREFOX_PROFILE_DIR o instala Firefox."
        )

    db_path = profile / "cookies.sqlite"
    if not db_path.exists():
        raise RuntimeError(f"No se encontro cookies.sqlite en {profile}")

    # Strategy: copy DB+WAL+SHM and open the copy in normal mode.
    # SQLite uses the SHM to index the WAL (where Firefox keeps fresh cookies
    # before flushing). This gives us the most up-to-date view.
    # Fallback: immutable read of the main DB file only.
    connection = None
    tmp_db = None
    try:
        tmp_db = _copy_cookie_db(profile)
        connection = sqlite3.connect(str(tmp_db))
    except (sqlite3.OperationalError, OSError):
        # Fallback: read main DB directly, skipping WAL data.
        if tmp_db:
            try:
                shutil.rmtree(tmp_db.parent, ignore_errors=True)
            except OSError:
                pass
            tmp_db = None
        try:
            connection = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"No se pudo abrir cookies.sqlite: {exc}") from exc

    try:
        cursor = connection.execute(
            "SELECT host, name, value, lastAccessed, expiry FROM moz_cookies"
        )
        now_unix = int(_now_seconds())
        # Each entry: (value, rank) with rank = (usable, specificity, lastAccessed).
        out: dict[str, tuple[str, tuple[int, int, int]]] = {}
        for host, name, value, last_accessed, expiry in cursor.fetchall():
            if not _host_matches(host, host_patterns):
                continue
            if not name or value is None:
                continue
            exp = int(expiry or 0)
            expired = 0 < exp <= now_unix
            if expired and not include_expired:
                continue
            score = _host_specificity_score(host, target_host) if target_host else 1
            if target_host and score <= 0:
                # Cookie does not actually apply to the target — skip.
                continue
            # A live, non-empty cookie ALWAYS beats an expired or blank one,
            # whatever its host specificity. Sites like QuickSight leave
            # blank expired "tombstones" on the exact host when the session
            # rotates, while the fresh token lives on the parent domain; the
            # browser never sends the tombstone, so neither must we
            # (verified 2026-10-21: QS 401 with a valid token in Firefox).
            usable = int(not expired and str(value) != "")
            rank = (usable, score, int(last_accessed or 0))
            existing = out.get(name)
            if existing is None or rank >= existing[1]:
                out[name] = (str(value), rank)
    finally:
        connection.close()
        if tmp_db:
            try:
                shutil.rmtree(tmp_db.parent, ignore_errors=True)
            except OSError:
                pass
    return {name: value for name, (value, _) in out.items()}


def _lz4_block_decompress(src: bytes) -> bytes:
    """Minimal pure-Python LZ4 *block* decompressor (no external deps).

    Enough to read Mozilla's ``jsonlz4`` session-store files. Follows the
    LZ4 block format: token (literal len / match len nibbles), literals,
    2-byte little-endian offset, match copy (min-match 4).
    """
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = token & 0x0F
        if mlen == 15:
            while i < n:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        if start < 0:
            break
        for j in range(mlen):
            out.append(out[start + j])
    return bytes(out)


def _read_mozlz4(path: Path) -> bytes:
    """Decompress a Mozilla ``mozLz40`` file (``sessionstore``/``recovery``)."""
    data = path.read_bytes()
    if data[:8] != b"mozLz40\x00":
        raise ValueError("Not a mozLz4 file")
    payload = data[12:]  # 8-byte magic + 4-byte LE decompressed size
    try:  # prefer the C module if it happens to be installed
        import lz4.block as _lz4block  # type: ignore

        size = int.from_bytes(data[8:12], "little")
        return _lz4block.decompress(payload, uncompressed_size=size)
    except Exception:  # noqa: BLE001 - fall back to the pure-python decoder
        return _lz4_block_decompress(payload)


def _sessionstore_cookie_record_sets(
    target_host: str,
    profile_dir: Path | None = None,
    *,
    partition_hosts: Iterable[str] = (),
) -> list[tuple[str, list[FirefoxCookie]]]:
    """Return each Firefox session snapshot as a separate cookie source."""
    profile = profile_dir or find_firefox_profile_dir()
    if profile is None:
        return []
    target = (target_host or "").strip().lower().lstrip(".")
    if not target:
        return []
    paths = [
        profile / "sessionstore-backups" / "recovery.jsonlz4",
        profile / "sessionstore-backups" / "recovery.baklz4",
        profile / "sessionstore.jsonlz4",
        profile / "sessionstore-backups" / "previous.jsonlz4",
    ]
    partition_patterns = tuple(partition_hosts)
    sources: list[tuple[str, list[FirefoxCookie]]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            raw = _read_mozlz4(path)
            data = json.loads(raw)
            snapshot_accessed = int(path.stat().st_mtime_ns // 1_000)
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        seen: set[tuple[str, str, str, str, str]] = set()
        records: list[FirefoxCookie] = []
        for cookie in data.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            host = str(cookie.get("host", "")).lower()
            bare_host = host.lstrip(".")
            raw_origin_attrs = cookie.get("originAttributes") or ""
            origin_attrs = (
                json.dumps(raw_origin_attrs, sort_keys=True, separators=(",", ":"))
                if isinstance(raw_origin_attrs, dict)
                else str(raw_origin_attrs)
            )
            direct = bool(
                bare_host and (target == bare_host or target.endswith("." + bare_host))
            )
            partitioned = bool(
                partition_patterns
                and _host_matches(_partition_host(origin_attrs), partition_patterns)
            )
            if not direct and not partitioned:
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            cookie_path = str(cookie.get("path") or "/")
            key = (str(name), str(value), host, cookie_path, origin_attrs)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                FirefoxCookie(
                    name=str(name),
                    value=str(value),
                    host=host,
                    path=cookie_path,
                    expiry=int(cookie.get("expiry") or 0),
                    last_accessed=int(cookie.get("lastAccessed") or snapshot_accessed),
                    origin_attributes=origin_attrs,
                    secure=bool(cookie.get("secure", True)),
                )
            )
        sources.append((path.name, records))
    return sources


def read_sessionstore_cookie_records(
    target_host: str,
    profile_dir: Path | None = None,
    *,
    partition_hosts: Iterable[str] = (),
) -> list[FirefoxCookie]:
    """Return live session cookies with their original Firefox scope."""
    out: list[FirefoxCookie] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for _source, records in _sessionstore_cookie_record_sets(
        target_host,
        profile_dir=profile_dir,
        partition_hosts=partition_hosts,
    ):
        for record in records:
            key = (
                record.name,
                record.value,
                record.host,
                record.path,
                record.origin_attributes,
            )
            if key not in seen:
                seen.add(key)
                out.append(record)
    return out


def read_sessionstore_cookies(
    target_host: str,
    profile_dir: Path | None = None,
) -> dict[str, str]:
    """Return in-memory (session-scoped) cookies for ``target_host``.

    Firefox keeps session cookies (those without a persistent expiry) only
    in memory; they never land in ``cookies.sqlite``. When "restore previous
    session" is active Firefox mirrors the live tab state — including those
    cookies — into ``sessionstore-backups/recovery.jsonlz4`` a few seconds
    apart. We read that snapshot to recover cookies such as the Flask
    ``session`` cookie that gate some apps. Pure local file read, same class
    as :func:`read_cookies_from_profile`.
    """
    out: dict[str, str] = {}
    for cookie in read_sessionstore_cookie_records(target_host, profile_dir):
        out.setdefault(cookie.name, cookie.value)
    return out


def debug_dump_cookies(host_patterns: Iterable[str]) -> list[dict[str, object]]:
    """Return raw rows (host, name, len(value), lastAccessed, expiry, is_expired)."""
    profile = find_firefox_profile_dir()
    if profile is None:
        return []
    tmp_db = _copy_cookie_db(profile)
    rows: list[dict[str, object]] = []
    now_unix = int(_now_seconds())
    try:
        connection = sqlite3.connect(f"file:{tmp_db}?mode=rw", uri=True)
    except sqlite3.OperationalError:
        connection = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT host, name, value, lastAccessed, expiry FROM moz_cookies"
        )
        for host, name, value, last_accessed, expiry in cursor.fetchall():
            if not _host_matches(host, host_patterns):
                continue
            exp = int(expiry or 0)
            rows.append(
                {
                    "host": host,
                    "name": name,
                    "value_length": len(str(value or "")),
                    "last_accessed": int(last_accessed or 0),
                    "expiry": exp,
                    "expired": 0 < exp <= now_unix,
                }
            )
    finally:
        connection.close()
        try:
            shutil.rmtree(tmp_db.parent, ignore_errors=True)
        except OSError:
            pass
    return rows


def harmony_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    """Cookies that Firefox would send to the Customer Calling API."""
    return read_cookies_from_profile(
        HARMONY_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host=HARMONY_API_HOST,
    )


def pod_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    return read_cookies_from_profile(
        POD_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host="honeypot.amazon.dev",
    )


def has_required(cookies: dict[str, str], required: Iterable[str]) -> bool:
    return any(name in cookies and cookies[name] for name in required)


def perfectmile_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    return read_cookies_from_profile(
        PERFECTMILE_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host="perfectmile.amazon.com",
    )


def routingtools_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    """Cookies for the RoutingTools portal (downloadManifest endpoint)."""
    return read_cookies_from_profile(
        ROUTINGTOOLS_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host="routingtools.amazon.com",
    )


def routediver_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    """Cookies for Route Diver / RouteIQ (``amzn_sso_token`` Midway JWT)."""
    return read_cookies_from_profile(
        ROUTEDIVER_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host="eu.eagle.routeiq.last-mile.amazon.dev",
    )


def relay_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    """Cookies for the Amazon Relay tracker (``amzn_sso_token`` Midway JWT)."""
    return read_cookies_from_profile(
        RELAY_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host="track.relay.amazon.dev",
    )


def logistics_cookies_from_profile(
    profile_dir: Path | None = None,
    *,
    target_host: str = "logistics.amazon.es",
) -> dict[str, str]:
    """Cookies for the Cortex portal (``logistics-federate-token`` Midway JWT)."""
    return read_cookies_from_profile(
        LOGISTICS_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host=target_host,
    )


def midway_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    """Cookies for the Midway SSO endpoint (used to mint OIDC id_tokens)."""
    return read_cookies_from_profile(
        MIDWAY_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host="midway-auth.amazon.com",
    )


def cwdash_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    """Cookies for the CloudWatch internal dashboards console (Midway)."""
    return read_cookies_from_profile(
        CWDASH_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host=CWDASH_HOST,
    )


def quicksight_cookies_from_profile(
    profile_dir: Path | None = None,
    *,
    target_host: str = "us-east-1.quicksight.aws.amazon.com",
) -> dict[str, str]:
    """Cookies for the QuickSight web session (AWS console federation)."""
    return read_cookies_from_profile(
        QUICKSIGHT_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host=target_host,
    )


def horizon_cookies_from_profile(profile_dir: Path | None = None) -> dict[str, str]:
    """Cookies for the Horizon data API (``amzn_sso_token`` Midway JWT).

    Scoped to ``horizon.eu-west-1.api.harmony.a2z.com`` so we read the
    host-only SSO cookies that authorize ``POST /v1/sso/assume``.
    """
    return read_cookies_from_profile(
        HORIZON_HOST_PATTERNS,
        profile_dir=profile_dir,
        target_host=HORIZON_API_HOST,
    )


def _sharepoint_cookie_sets(
    records: Iterable[FirefoxCookie],
    host: str,
) -> list[tuple[tuple[int, int, int, str], str, dict[str, str]]]:
    """Build deterministic coherent auth sets, one per Firefox partition."""
    now_unix = int(_now_seconds())
    grouped: dict[str, list[FirefoxCookie]] = {}
    for record in records:
        if 0 < record.expiry <= now_unix:
            continue
        if _host_specificity_score(record.host, host) <= 0:
            continue
        grouped.setdefault(record.origin_attributes, []).append(record)

    candidates: list[tuple[tuple[int, int, int, str], str, dict[str, str]]] = []
    for origin_attributes, group in grouped.items():
        fedauth = [
            item
            for item in group
            if item.name == "FedAuth"
            and item.host.lower().lstrip(".") == host
        ]
        if not fedauth:
            continue
        fed = max(fedauth, key=lambda item: (item.last_accessed, item.value))
        rtfa = [item for item in group if item.name == "rtFa"]
        pair = max(rtfa, key=lambda item: (item.last_accessed, item.value)) if rtfa else None
        pair_freshness = min(
            fed.last_accessed,
            pair.last_accessed if pair is not None else fed.last_accessed,
        )

        selected: dict[str, tuple[str, int, int]] = {}
        for item in group:
            specificity = _host_specificity_score(item.host, host)
            current = selected.get(item.name)
            if current is None or (specificity, item.last_accessed, item.value) > (
                current[1],
                current[2],
                current[0],
            ):
                selected[item.name] = (item.value, specificity, item.last_accessed)
        cookies = {name: value for name, (value, _score, _seen) in selected.items()}
        cookies["FedAuth"] = fed.value
        if pair is not None:
            cookies["rtFa"] = pair.value
        fingerprint = hashlib.sha256(
            (fed.value + "\0" + (pair.value if pair is not None else "")).encode()
        ).hexdigest()
        rank = (
            1 if pair is not None else 0,
            pair_freshness,
            fed.last_accessed,
            fingerprint,
        )
        candidates.append((rank, origin_attributes, cookies))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


def _sharepoint_cookie_set(
    records: Iterable[FirefoxCookie],
    host: str,
) -> dict[str, str]:
    """Choose the highest-ranked coherent SharePoint cookie partition."""
    candidates = _sharepoint_cookie_sets(records, host)
    return candidates[0][2] if candidates else {}


def sharepoint_cookie_candidates(
    host: str,
    profile_dir: Path | None = None,
) -> list[SharePointCookieCandidate]:
    """Return every coherent auth pair from the first applicable profile.

    Persistent cookies and the Firefox session snapshot remain separate auth
    candidates. This is important because SharePoint can invalidate one pair
    while Firefox still retains it with a newer ``lastAccessed`` than the pair
    used by the active tab. Callers can therefore try the deterministic first
    candidate and fall back once after an actual read-only auth rejection.
    """
    host = (host or "").strip().lower().lstrip(".")
    if not host:
        return []
    patterns = (host, "." + host, ".sharepoint.com")
    profiles = [Path(profile_dir)] if profile_dir is not None else _candidate_profile_dirs()
    for profile in profiles:
        if not (profile / "cookies.sqlite").exists():
            continue
        sources: list[tuple[str, list[FirefoxCookie]]] = []
        try:
            sources.append(
                (
                    "sqlite",
                    read_cookie_records_from_profile(
                        patterns,
                        profile_dir=profile,
                        target_host=host,
                    ),
                )
            )
        except (OSError, RuntimeError, sqlite3.Error):
            pass
        try:
            session_sources = _sessionstore_cookie_record_sets(
                host,
                profile_dir=profile,
            )
            for index, (snapshot_name, records) in enumerate(session_sources):
                source_name = "sessionstore" if index == 0 else f"sessionstore:{snapshot_name}"
                sources.append((source_name, records))
        except (OSError, RuntimeError, ValueError):
            pass

        all_records = [record for _source, records in sources for record in records]
        extras_by_partition: dict[str, dict[str, str]] = {}
        for _rank, origin_attributes, cookies in _sharepoint_cookie_sets(all_records, host):
            extras_by_partition[origin_attributes] = {
                name: value
                for name, value in cookies.items()
                if name not in {"FedAuth", "rtFa"}
            }

        found: dict[str, SharePointCookieCandidate] = {}
        for source, records in sources:
            for rank, origin_attributes, cookies in _sharepoint_cookie_sets(records, host):
                complete = {
                    **extras_by_partition.get(origin_attributes, {}),
                    **cookies,
                }
                fingerprint = hashlib.sha256(
                    (
                        complete.get("FedAuth", "")
                        + "\0"
                        + complete.get("rtFa", "")
                    ).encode()
                ).hexdigest()
                candidate = SharePointCookieCandidate(
                    cookies=complete,
                    fingerprint=fingerprint,
                    profile=profile,
                    source=source,
                    origin_attributes=origin_attributes,
                    rank=rank,
                )
                previous = found.get(fingerprint)
                if previous is None or candidate.rank > previous.rank:
                    found[fingerprint] = candidate
        if found:
            return sorted(found.values(), key=lambda item: item.rank, reverse=True)
    return []


def sharepoint_cookies_from_profile(
    host: str,
    profile_dir: Path | None = None,
) -> dict[str, str]:
    """Return the deterministic first coherent SharePoint cookie candidate."""
    candidates = sharepoint_cookie_candidates(host, profile_dir=profile_dir)
    return dict(candidates[0].cookies) if candidates else {}


# ---------------------------------------------------------------------------
# Session-expiry helpers
# ---------------------------------------------------------------------------


def jwt_is_expired(token: str, *, grace_seconds: int = 30) -> bool:
    """Return True when a JWT/JWS ``token`` has an ``exp`` claim in the past.

    The Amazon SSO cookies (``amzn_sso_token``, ``logistics-federate-token``)
    are JWS strings with a JSON payload in the second dot-separated segment.
    We parse ``exp`` without pulling in PyJWT to keep the dependency surface
    small. Anything we cannot parse is treated as *not* expired (we only use
    this to produce a clearer "re-login" message, never to block a call that
    the real request would otherwise attempt).

    ``grace_seconds`` avoids flagging a token that is a few seconds from a
    refresh.
    """
    if not token:
        return False
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return False
        payload_b64 = parts[1]
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
    return exp_int < (_now_seconds() - grace_seconds)


def session_status(
    host_patterns: Iterable[str],
    required: Iterable[str],
    *,
    target_host: str | None = None,
    profile_dir: Path | None = None,
    jwt_cookies: Iterable[str] = ("amzn_sso_token", "logistics-federate-token"),
) -> dict[str, object]:
    """Classify a Firefox-cookie session as ready / expired / missing.

    Reads the profile twice: once normally (expired cookies dropped) and
    once including expired cookies. This lets us tell apart:

    * **missing** — the required cookie was never there (never logged in).
    * **expired** — the cookie is present on disk but its ``exp`` has
      passed (logged in once, session lapsed → needs re-login).

    Returns ``{ready, missing, expired, cookie_count}`` where ``expired`` is
    the list of required cookie names that are present-but-expired. A name is
    never in both ``missing`` and ``expired``.
    """
    required = list(required)
    try:
        live = read_cookies_from_profile(
            host_patterns, profile_dir=profile_dir, target_host=target_host
        )
    except Exception:  # noqa: BLE001
        live = {}
    try:
        with_expired = read_cookies_from_profile(
            host_patterns,
            profile_dir=profile_dir,
            target_host=target_host,
            include_expired=True,
        )
    except Exception:  # noqa: BLE001
        with_expired = dict(live)

    jwt_set = set(jwt_cookies)
    missing: list[str] = []
    expired: list[str] = []
    for name in required:
        if name in live and live[name]:
            # Present and not filtered as expired by the date check. For JWT
            # cookies double-check the embedded ``exp`` (Firefox occasionally
            # keeps a stale session cookie with expiry=0).
            if name in jwt_set and jwt_is_expired(live[name]):
                expired.append(name)
            continue
        # Not in the live read. Was it dropped because it expired?
        if name in with_expired and with_expired[name]:
            expired.append(name)
        else:
            missing.append(name)

    return {
        "ready": not missing and not expired,
        "missing": missing,
        "expired": expired,
        "cookie_count": len(live),
    }
