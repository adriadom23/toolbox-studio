from __future__ import annotations

import ipaddress
import json
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_DIR.parent
DEFAULT_CONFIG_PATH = BACKEND_DIR / "config" / "studio.config.json"
DEFAULT_DATABASE_PATH = BACKEND_DIR / "data" / "runtime" / "studio.sqlite3"
_ALLOWED_ROLES = {"admin", "reviewer", "submitter"}
_ALLOWED_TOP_LEVEL = {
    "schema_version", "server", "identity", "runtime", "providers", "storage",
    "build", "logs", "notifications", "preview", "publication", "git", "legacy",
    "file_policy", "compat", "owners", "limits",
}


@dataclass(frozen=True)
class StudioConfig:
    profile: str = "local"
    host: str = "127.0.0.1"
    port: int = 5000
    database_path: Path = DEFAULT_DATABASE_PATH
    proxy_hmac_secret: str | None = None
    trusted_peer_cidrs: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    identity_mapping_version: int = 1
    identity_mapping: dict[str, str] = field(default_factory=dict)
    local_user_id: str = "local-user"
    local_display_name: str = "Propietario local"
    identity_roles: dict[str, tuple[str, ...]] = field(default_factory=lambda: {"local-user": ("admin", "reviewer", "submitter")})
    review_base_url: str = "http://127.0.0.1:5173"
    public_base_url: str = "http://127.0.0.1:5173"
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173",)
    artifact_provider: str = "local"
    build_provider: str = "local-subprocess"
    log_provider: str = "local"
    notification_providers: tuple[str, ...] = ("local",)
    publication_provider: str = "local"
    runtime_provider: str = "local-subprocess"
    preview_host: str = "127.0.0.1"
    preview_port: int = 5001
    preview_origin: str = "http://127.0.0.1:5001"
    preview_allowed_origins: tuple[str, ...] = ()
    s3_bucket: str | None = None
    s3_prefix: str = "toolbox/artifacts"
    codebuild_project: str | None = None
    cloudwatch_log_group: str | None = None
    ses_source: str | None = None
    ses_recipients: tuple[str, ...] = ()
    teams_webhook_secret_env: str | None = None
    teams_allowed_hosts: tuple[str, ...] = ()
    cursor_hmac_secret: str = "local-cursor-key"
    git_require_clean: bool = False
    file_exclusions: tuple[str, ...] = ()
    submit_wait_seconds: int = 900
    request_body_limit_bytes: int = 2 * 1024 * 1024
    editor_body_limit_bytes: int = 12 * 1024 * 1024
    manifest_upload_limit_bytes: int = 12 * 1024 * 1024
    snapshot_max_files: int = 10_000
    snapshot_max_bytes: int = 100 * 1024 * 1024
    artifact_max_bytes: int = 100 * 1024 * 1024
    log_phase_max_bytes: int = 5 * 1024 * 1024
    build_timeout_seconds: int = 900
    max_builds_global: int = 2
    max_builds_per_project: int = 1
    storage_spool_max_bytes: int = 2 * 1024 * 1024 * 1024
    storage_min_free_bytes: int = 1024 * 1024 * 1024
    codebuild_network_preflight: str | None = None
    codebuild_image_digest: str | None = None
    codebuild_wrapper_sha256: str | None = None
    codebuild_probe_version: str | None = None
    codebuild_account_id: str | None = None
    codebuild_region: str | None = None
    runtime_gateway_url: str | None = None
    runtime_gateway_token_env: str | None = None
    runtime_gateway_allowed_hosts: tuple[str, ...] = ()

    @property
    def is_local(self) -> bool:
        return self.profile == "local"

    def peer_is_trusted(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
            return any(address in ipaddress.ip_network(value, strict=False) for value in self.trusted_peer_cidrs)
        except ValueError:
            return False

    def validate_runtime_safety(self) -> None:
        normalize_identity(self.local_user_id, "identity.user_id")
        normalize_display_name(self.local_display_name, "identity.display_name")
        public_url = validate_safe_url(
            self.public_base_url,
            "server.public_base_url",
            https_required=not self.is_local,
        )
        review_url = validate_safe_url(
            self.review_base_url,
            "server.public_base_url",
            https_required=not self.is_local,
            allowed_hosts={urlparse(public_url).hostname or ""},
        )
        if review_url != public_url:
            raise ValueError("config_invalid: review URL must match server.public_base_url")
        for origin in self.cors_origins:
            validate_safe_url(origin, "server.cors_origins", https_required=not self.is_local, exact_origin=True)
        preview_url = validate_safe_url(
            self.preview_origin,
            "preview.origin",
            https_required=not self.is_local,
            exact_origin=True,
        )
        parsed_public = urlparse(public_url)
        parsed_preview = urlparse(preview_url)
        public_port = parsed_public.port or (443 if parsed_public.scheme == "https" else 80)
        preview_port = parsed_preview.port or (443 if parsed_preview.scheme == "https" else 80)
        if (parsed_public.scheme, parsed_public.hostname, public_port) == (parsed_preview.scheme, parsed_preview.hostname, preview_port):
            raise ValueError("config_invalid: preview.origin must be separate from server.public_base_url")
        if self.is_local:
            loopback_hosts = {"127.0.0.1", "::1", "localhost"}
            if self.host not in loopback_hosts:
                raise ValueError("config_invalid: local profile must bind to loopback")
            if parsed_public.hostname not in loopback_hosts:
                raise ValueError("config_invalid: local server.public_base_url must use a loopback host")
            if self.preview_host not in loopback_hosts or parsed_preview.hostname not in loopback_hosts:
                raise ValueError("config_invalid: local preview bind and origin must use loopback")
            if self.preview_host.casefold() != (parsed_preview.hostname or "").casefold() or self.preview_port != preview_port:
                raise ValueError("config_invalid: local preview bind/port must match preview.origin")
            return
        allowed_preview_origins = {
            validate_safe_url(origin, "preview.allowed_origins", https_required=True, exact_origin=True)
            for origin in self.preview_allowed_origins
        }
        if preview_url not in allowed_preview_origins:
            raise ValueError("config_invalid: shared preview.origin must be explicitly allowlisted by preview.allowed_origins")
        cors_hosts = {urlparse(origin).hostname.casefold() for origin in self.cors_origins if urlparse(origin).hostname}
        if (urlparse(public_url).hostname or "").casefold() not in cors_hosts:
            raise ValueError("config_invalid: server.public_base_url host must be allowlisted by server.cors_origins")
        missing: list[str] = []
        if self.build_provider != "codebuild":
            missing.append("providers.build=codebuild")
        if self.runtime_provider != "isolated-gateway":
            missing.append("providers.runtime=isolated-gateway")
        if self.artifact_provider != "s3":
            missing.append("providers.artifact=s3")
        if not self.proxy_hmac_secret:
            missing.append("identity.proxy_hmac_secret_env")
        if not self.trusted_peer_cidrs:
            missing.append("identity.trusted_peer_cidrs")
        if not self.codebuild_image_digest:
            missing.append("build.approved_image_digest")
        if not self.codebuild_wrapper_sha256:
            missing.append("build.approved_wrapper_sha256")
        if not self.codebuild_probe_version:
            missing.append("build.probe_version")
        if not self.codebuild_account_id:
            missing.append("build.account_id")
        if not self.codebuild_region:
            missing.append("build.region")
        if missing:
            raise ValueError("config_invalid: shared profile requires isolated providers and trusted proxy settings: " + ", ".join(missing))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("config_invalid: configuration is unreadable or is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("config_invalid: configuration root must be an object")
    return value


def _object(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config_invalid: {name} must be an object")
    return value


def _closed(value: dict[str, Any], field_name: str, allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"config_invalid: unsupported {field_name} fields: " + ", ".join(sorted(unknown)))


def validate_safe_url(
    value: object,
    field_name: str,
    *,
    https_required: bool = False,
    exact_origin: bool = False,
    allowed_hosts: set[str] | None = None,
) -> str:
    text = str(value).strip()
    if not text or text != unicodedata.normalize("NFKC", text) or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"config_invalid: {field_name} must be an unambiguous absolute URL")
    parsed = urlparse(text)
    allowed_schemes = {"https"} if https_required else {"http", "https"}
    host = parsed.hostname.casefold() if parsed.hostname else None
    if (
        parsed.scheme not in allowed_schemes
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in text
        or (exact_origin and parsed.path not in {"", "/"})
        or (allowed_hosts is not None and host not in {item.casefold() for item in allowed_hosts})
    ):
        raise ValueError(f"config_invalid: {field_name} must be an absolute safe URL without credentials, query, or fragment")
    return text.rstrip("/")


def _url(value: object, field_name: str, *, https_required: bool = False, exact_origin: bool = False) -> str:
    return validate_safe_url(value, field_name, https_required=https_required, exact_origin=exact_origin)


def _legacy_v1(raw: dict[str, Any]) -> bool:
    return bool(raw) and set(raw) <= {"owners", "notifications"} and isinstance(raw.get("owners", []), list)


def _resolve_runtime_path(value: object, configured_path: Path) -> Path:
    candidate = Path(str(value))
    if candidate.is_absolute():
        return candidate.resolve()
    try:
        configured_path.resolve().relative_to(REPOSITORY_ROOT)
        base = REPOSITORY_ROOT
    except ValueError:
        base = configured_path.resolve().parent
    return (base / candidate).resolve()


def normalize_identity(value: object, field_name: str = "identity") -> str:
    if not isinstance(value, str):
        raise ValueError(f"config_invalid: {field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not 1 <= len(normalized) <= 200
        or normalized != value
        or unicodedata.normalize("NFKC", normalized) != normalized
        or normalized != normalized.strip()
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ValueError(f"config_invalid: {field_name} must be an unambiguous identity from 1 to 200 characters")
    return normalized


def normalize_display_name(value: object, field_name: str = "display name") -> str:
    if not isinstance(value, str):
        raise ValueError(f"config_invalid: {field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not 1 <= len(normalized) <= 200
        or normalized != value
        or unicodedata.normalize("NFKC", normalized) != normalized
        or normalized != normalized.strip()
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ValueError(f"config_invalid: {field_name} must be an unambiguous display name from 1 to 200 characters")
    return normalized


def load_studio_config(path: Path | None = None) -> StudioConfig:
    configured_path = Path(path or os.environ.get("STUDIO_CONFIG_PATH", DEFAULT_CONFIG_PATH)).resolve()
    raw = _read_json(configured_path)
    if _legacy_v1(raw):
        return StudioConfig()
    if raw and raw.get("schema_version") != 2:
        raise ValueError("config_invalid: expected exact legacy v1 or schema_version 2")
    unknown = set(raw) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ValueError("config_invalid: unsupported top-level fields: " + ", ".join(sorted(unknown)))

    server = _object(raw, "server")
    identity = _object(raw, "identity")
    runtime = _object(raw, "runtime")
    providers = _object(raw, "providers")
    storage = _object(raw, "storage")
    build = _object(raw, "build")
    logs = _object(raw, "logs")
    notifications = _object(raw, "notifications")
    preview = _object(raw, "preview")
    publication = _object(raw, "publication")
    git = _object(raw, "git")
    legacy = _object(raw, "legacy")
    file_policy = _object(raw, "file_policy")
    compat = _object(raw, "compat")
    limits = _object(raw, "limits")
    _closed(server, "server", {"profile", "bind", "port", "public_base_url", "cors_origins"})
    _closed(identity, "identity", {"user_id", "display_name", "mapping_version", "mapping", "roles", "trusted_peer_cidrs", "proxy_hmac_secret_env", "cursor_hmac_secret"})
    _closed(runtime, "runtime", {"root", "database_path", "gateway_url", "gateway_token_env", "gateway_allowed_hosts"})
    _closed(providers, "providers", {"artifact", "build", "logs", "notifications", "publication", "runtime"})
    _closed(storage, "storage", {"s3_bucket", "s3_prefix"})
    _closed(build, "build", {
        "codebuild_project", "network_preflight", "approved_image_digest",
        "approved_wrapper_sha256", "probe_version", "account_id", "region",
    })
    _closed(logs, "logs", {"cloudwatch_group"})
    _closed(notifications, "notifications", {"ses_source", "ses_recipients", "teams_webhook_secret_env", "teams_allowed_hosts"})
    _closed(preview, "preview", {"bind", "origin", "allowed_origins"})
    _closed(publication, "publication", {"targets"})
    _closed(git, "git", {"provider", "require_clean", "existing_access_confirmed"})
    _closed(legacy, "legacy", {"root"})
    _closed(file_policy, "file_policy", {"exclusions"})
    _closed(compat, "compat", {"enable_v1_notifications", "submit_wait_seconds"})
    _closed(limits, "limits", {
        "request_body_bytes", "editor_body_bytes", "manifest_upload_body_bytes",
        "snapshot_files", "snapshot_bytes", "artifact_bytes", "log_phase_bytes",
        "build_timeout_seconds", "builds_global", "builds_per_project",
        "storage_spool_bytes", "storage_min_free_bytes",
    })

    profile = str(os.environ.get("STUDIO_PROFILE", server.get("profile", "local"))).strip().lower()
    if profile not in {"local", "shared"}:
        raise ValueError("config_invalid: server.profile must be local or shared")
    host = str(os.environ.get("STUDIO_HOST", server.get("bind", "127.0.0.1"))).strip()
    public_base_url = _url(server.get("public_base_url", "http://127.0.0.1:5173"), "server.public_base_url", https_required=profile == "shared")
    parsed_public = urlparse(public_base_url)
    if profile == "local" and parsed_public.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("config_invalid: local server.public_base_url must use a loopback host")
    cors = server.get("cors_origins", ["http://127.0.0.1:5173"])
    if not isinstance(cors, list) or not cors or "*" in cors:
        raise ValueError("config_invalid: server.cors_origins must be a non-empty exact-origin list")
    cors_origins = tuple(_url(item, "server.cors_origins", https_required=profile == "shared", exact_origin=True) for item in cors)
    if profile == "shared" and parsed_public.hostname.casefold() not in {urlparse(origin).hostname.casefold() for origin in cors_origins}:
        raise ValueError("config_invalid: server.public_base_url host must be allowlisted by server.cors_origins")

    mapping_version = int(identity.get("mapping_version", 1))
    if mapping_version != 1:
        raise ValueError("config_invalid: unsupported identity mapping version")
    mapping = identity.get("mapping", {})
    if profile == "local":
        configured_local_user_id = normalize_identity(identity.get("user_id", "local-user"), "identity.user_id")
        local_user_id = normalize_identity(
            os.environ.get("STUDIO_LOCAL_USER_ID", configured_local_user_id),
            "identity.user_id",
        )
        local_display_name = normalize_display_name(
            os.environ.get("STUDIO_LOCAL_DISPLAY_NAME", identity.get("display_name", "Propietario local")),
            "identity.display_name",
        )
    else:
        if "user_id" in identity or "display_name" in identity:
            raise ValueError("config_invalid: identity.user_id and identity.display_name are only valid for the local profile")
        configured_local_user_id = "local-user"
        local_user_id = "local-user"
        local_display_name = "Propietario local"
    default_roles = {local_user_id: ["admin", "reviewer", "submitter"]} if profile == "local" else {}
    raw_roles = identity.get("roles", default_roles)
    if (
        profile == "local"
        and local_user_id != configured_local_user_id
        and isinstance(raw_roles, dict)
        and configured_local_user_id in raw_roles
        and local_user_id not in raw_roles
    ):
        raw_roles = {**raw_roles, local_user_id: raw_roles[configured_local_user_id]}
        raw_roles.pop(configured_local_user_id)
    peers = identity.get("trusted_peer_cidrs", ["127.0.0.1/32", "::1/128"] if profile == "local" else [])
    if not isinstance(mapping, dict) or not isinstance(raw_roles, dict) or any(not isinstance(value, list) for value in raw_roles.values()):
        raise ValueError("config_invalid: identity mapping/roles are invalid")
    normalized_mapping = {
        normalize_identity(key, "identity.mapping key"): normalize_identity(value, "identity.mapping value")
        for key, value in mapping.items()
    }
    normalized_role_actors = {
        normalize_identity(actor, "identity.roles key"): values
        for actor, values in raw_roles.items()
    }
    if not isinstance(peers, list):
        raise ValueError("config_invalid: identity.trusted_peer_cidrs must be an array")
    for peer in peers:
        try:
            ipaddress.ip_network(str(peer), strict=False)
        except ValueError as exc:
            raise ValueError("config_invalid: identity.trusted_peer_cidrs contains an invalid network") from exc
    roles = {actor: tuple(str(role) for role in values) for actor, values in normalized_role_actors.items()}
    if any(role not in _ALLOWED_ROLES for values in roles.values() for role in values):
        raise ValueError("config_invalid: identity.roles contains an unsupported role")
    secret_env = identity.get("proxy_hmac_secret_env")
    if secret_env is not None and (not isinstance(secret_env, str) or not secret_env):
        raise ValueError("config_invalid: identity.proxy_hmac_secret_env must name an environment variable")
    secret = os.environ.get(str(secret_env)) if secret_env else os.environ.get("STUDIO_PROXY_HMAC_SECRET")
    teams_hosts = notifications.get("teams_allowed_hosts", [])
    if not isinstance(teams_hosts, list) or any(not isinstance(host, str) or not host.strip() for host in teams_hosts):
        raise ValueError("config_invalid: notifications.teams_allowed_hosts must be an array of host names")
    git_require_clean = git.get("require_clean", False)
    if not isinstance(git_require_clean, bool):
        raise ValueError("config_invalid: git.require_clean must be a boolean")
    exclusions = file_policy.get("exclusions", [])
    if (
        not isinstance(exclusions, list)
        or len(exclusions) > 100
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 100
            or item in {".", ".."}
            or "/" in item
            or "\\" in item
            for item in exclusions
        )
    ):
        raise ValueError("config_invalid: file_policy.exclusions must contain up to 100 entry names")
    submit_wait_seconds = compat.get("submit_wait_seconds", 900)
    if isinstance(submit_wait_seconds, bool) or not isinstance(submit_wait_seconds, int) or not 1 <= submit_wait_seconds <= 3600:
        raise ValueError("config_invalid: compat.submit_wait_seconds must be an integer from 1 to 3600")

    limit_defaults = {
        "request_body_bytes": 2 * 1024 * 1024,
        "editor_body_bytes": 12 * 1024 * 1024,
        "manifest_upload_body_bytes": 12 * 1024 * 1024,
        "snapshot_files": 10_000,
        "snapshot_bytes": 100 * 1024 * 1024,
        "artifact_bytes": 100 * 1024 * 1024,
        "log_phase_bytes": 5 * 1024 * 1024,
        "build_timeout_seconds": 900,
        "builds_global": 2,
        "builds_per_project": 1,
        "storage_spool_bytes": 2 * 1024 * 1024 * 1024,
        "storage_min_free_bytes": 1024 * 1024 * 1024,
    }
    parsed_limits: dict[str, int] = {}
    for name, default in limit_defaults.items():
        value = limits.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"config_invalid: limits.{name} must be a positive integer")
        parsed_limits[name] = value
    if parsed_limits["builds_per_project"] > parsed_limits["builds_global"]:
        raise ValueError("config_invalid: limits.builds_per_project cannot exceed limits.builds_global")
    if parsed_limits["snapshot_bytes"] + parsed_limits["artifact_bytes"] > parsed_limits["storage_spool_bytes"]:
        raise ValueError("config_invalid: storage spool must hold one snapshot and artifact reservation")

    network_preflight = build.get("network_preflight")
    if network_preflight is not None and network_preflight != "aws-vpc-endpoints-v1":
        raise ValueError("config_invalid: build.network_preflight is not allowlisted")
    image_digest = build.get("approved_image_digest")
    wrapper_sha256 = build.get("approved_wrapper_sha256")
    probe_version = build.get("probe_version")
    account_id = build.get("account_id")
    region = build.get("region")
    for field_name, value in (
        ("build.approved_image_digest", image_digest),
        ("build.approved_wrapper_sha256", wrapper_sha256),
    ):
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"config_invalid: {field_name} must be an exact lowercase SHA-256")
    if probe_version is not None and (
        not isinstance(probe_version, str)
        or not 1 <= len(probe_version) <= 100
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in probe_version)
    ):
        raise ValueError("config_invalid: build.probe_version is invalid")
    if account_id is not None and (not isinstance(account_id, str) or len(account_id) != 12 or not account_id.isdigit()):
        raise ValueError("config_invalid: build.account_id must be a 12-digit AWS account ID")
    if region is not None and (
        not isinstance(region, str)
        or not 3 <= len(region) <= 32
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in region)
    ):
        raise ValueError("config_invalid: build.region is invalid")
    gateway_url = runtime.get("gateway_url")
    gateway_token_env = runtime.get("gateway_token_env")
    gateway_hosts = runtime.get("gateway_allowed_hosts", [])
    if gateway_url is not None:
        gateway_url = _url(gateway_url, "runtime.gateway_url", https_required=True)
    if gateway_token_env is not None and (not isinstance(gateway_token_env, str) or not gateway_token_env):
        raise ValueError("config_invalid: runtime.gateway_token_env must name an environment variable")
    if not isinstance(gateway_hosts, list) or any(not isinstance(item, str) or not item.strip() for item in gateway_hosts):
        raise ValueError("config_invalid: runtime.gateway_allowed_hosts must be an array of host names")
    normalized_gateway_hosts = tuple(item.strip().casefold() for item in gateway_hosts)
    if gateway_url and urlparse(gateway_url).hostname.casefold() not in normalized_gateway_hosts:
        raise ValueError("config_invalid: runtime gateway host must be explicitly allowlisted")

    root = _resolve_runtime_path(runtime.get("root", DEFAULT_DATABASE_PATH.parent), configured_path)
    database_raw = os.environ.get("STUDIO_DATABASE_PATH", runtime.get("database_path", root / "submissions.sqlite3"))
    database = _resolve_runtime_path(database_raw, configured_path)
    preview_bind = str(preview.get("bind", "127.0.0.1")).strip()
    preview_origin = _url(preview.get("origin", "http://127.0.0.1:5001"), "preview.origin", https_required=profile == "shared", exact_origin=True)
    preview_allowlist = preview.get("allowed_origins", [])
    if not isinstance(preview_allowlist, list) or any(not isinstance(item, str) for item in preview_allowlist):
        raise ValueError("config_invalid: preview.allowed_origins must be an array of exact origins")
    preview_allowed_origins = tuple(
        _url(item, "preview.allowed_origins", https_required=profile == "shared", exact_origin=True)
        for item in preview_allowlist
    )
    parsed_preview = urlparse(preview_origin)
    preview_port = parsed_preview.port or (443 if parsed_preview.scheme == "https" else 80)
    if profile == "local" and preview_bind not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("config_invalid: local preview must bind to loopback")
    artifact_kind = str(providers.get("artifact", "local"))
    build_kind = str(providers.get("build", "local-subprocess"))
    runtime_kind = str(providers.get("runtime", "local-subprocess"))
    log_kind = str(providers.get("logs", "local"))
    notification_kinds = providers.get("notifications", ["local"])
    if artifact_kind not in {"local", "s3"} or build_kind not in {"local-subprocess", "codebuild"} or runtime_kind not in {"local-subprocess", "isolated-gateway"} or log_kind not in {"local", "cloudwatch"}:
        raise ValueError("config_invalid: providers contain an unsupported provider")
    if str(providers.get("publication", "local")) != "local":
        raise ValueError("config_invalid: providers.publication is unsupported")
    if not isinstance(notification_kinds, list) or any(str(item) not in {"local", "ses", "teams"} for item in notification_kinds):
        raise ValueError("config_invalid: providers.notifications must contain supported providers")
    config = StudioConfig(
        profile=profile,
        host=host,
        port=int(os.environ.get("STUDIO_PORT", server.get("port", 5000))),
        database_path=database,
        proxy_hmac_secret=secret,
        trusted_peer_cidrs=tuple(str(item) for item in peers),
        identity_mapping_version=mapping_version,
        identity_mapping=normalized_mapping,
        local_user_id=local_user_id,
        local_display_name=local_display_name,
        identity_roles=roles,
        review_base_url=public_base_url,
        public_base_url=public_base_url,
        cors_origins=cors_origins,
        artifact_provider=artifact_kind,
        build_provider=build_kind,
        log_provider=log_kind,
        notification_providers=tuple(str(item) for item in notification_kinds),
        publication_provider=str(providers.get("publication", "local")),
        runtime_provider=runtime_kind,
        preview_host=preview_bind,
        preview_port=preview_port,
        preview_origin=preview_origin,
        preview_allowed_origins=preview_allowed_origins,
        s3_bucket=str(storage.get("s3_bucket")) if storage.get("s3_bucket") else None,
        s3_prefix=str(storage.get("s3_prefix", "toolbox/artifacts")),
        codebuild_project=str(build.get("codebuild_project")) if build.get("codebuild_project") else None,
        cloudwatch_log_group=str(logs.get("cloudwatch_group")) if logs.get("cloudwatch_group") else None,
        ses_source=str(notifications.get("ses_source")) if notifications.get("ses_source") else None,
        ses_recipients=tuple(str(item) for item in notifications.get("ses_recipients", [])),
        teams_webhook_secret_env=str(notifications.get("teams_webhook_secret_env")) if notifications.get("teams_webhook_secret_env") else None,
        teams_allowed_hosts=tuple(host.strip().casefold() for host in teams_hosts),
        cursor_hmac_secret=str(identity.get("cursor_hmac_secret", secret or "local-cursor-key")),
        git_require_clean=git_require_clean,
        file_exclusions=tuple(item.strip().casefold() for item in exclusions),
        submit_wait_seconds=submit_wait_seconds,
        request_body_limit_bytes=parsed_limits["request_body_bytes"],
        editor_body_limit_bytes=parsed_limits["editor_body_bytes"],
        manifest_upload_limit_bytes=parsed_limits["manifest_upload_body_bytes"],
        snapshot_max_files=parsed_limits["snapshot_files"],
        snapshot_max_bytes=parsed_limits["snapshot_bytes"],
        artifact_max_bytes=parsed_limits["artifact_bytes"],
        log_phase_max_bytes=parsed_limits["log_phase_bytes"],
        build_timeout_seconds=parsed_limits["build_timeout_seconds"],
        max_builds_global=parsed_limits["builds_global"],
        max_builds_per_project=parsed_limits["builds_per_project"],
        storage_spool_max_bytes=parsed_limits["storage_spool_bytes"],
        storage_min_free_bytes=parsed_limits["storage_min_free_bytes"],
        codebuild_network_preflight=str(network_preflight) if network_preflight else None,
        codebuild_image_digest=str(image_digest) if image_digest else None,
        codebuild_wrapper_sha256=str(wrapper_sha256) if wrapper_sha256 else None,
        codebuild_probe_version=str(probe_version) if probe_version else None,
        codebuild_account_id=str(account_id) if account_id else None,
        codebuild_region=str(region) if region else None,
        runtime_gateway_url=str(gateway_url) if gateway_url else None,
        runtime_gateway_token_env=str(gateway_token_env) if gateway_token_env else None,
        runtime_gateway_allowed_hosts=normalized_gateway_hosts,
    )
    config.validate_runtime_safety()
    if artifact_kind == "s3" and not config.s3_bucket:
        raise ValueError("config_invalid: storage.s3_bucket is required for S3")
    if build_kind == "codebuild" and not config.codebuild_project:
        raise ValueError("config_invalid: build.codebuild_project is required")
    if profile == "shared":
        if config.codebuild_network_preflight != "aws-vpc-endpoints-v1":
            raise ValueError("config_invalid: shared profile requires allowlisted build.network_preflight")
        if not config.runtime_gateway_url or not config.runtime_gateway_token_env or not config.runtime_gateway_allowed_hosts:
            raise ValueError("config_invalid: shared profile requires an allowlisted isolated runtime gateway")
        if not os.environ.get(config.runtime_gateway_token_env):
            raise ValueError("config_invalid: isolated runtime gateway token environment variable is missing")
    if "teams" in config.notification_providers and not config.teams_allowed_hosts:
        raise ValueError("config_invalid: notifications.teams_allowed_hosts is required for Teams")
    return config


def map_identity(config: StudioConfig, external_identity: str) -> str:
    if config.identity_mapping_version != 1:
        raise ValueError("unsupported identity mapping version")
    external = normalize_identity(external_identity, "proxy identity")
    return normalize_identity(config.identity_mapping.get(external, external), "mapped proxy identity")
