from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
from contextlib import ExitStack, asynccontextmanager, suppress
from pathlib import Path

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.routers.preview import create_preview_router
from app.api.routers.review import actor_roles, create_review_router
from app.api.routers.submissions import create_submissions_router
from app.api.schemas import (
    PROJECT_BATCH_MAX_BYTES,
    PROJECT_FILE_MAX_BYTES,
    EditableFile,
    OpaqueFile,
    ProjectClaim,
    ProjectCreate,
    ProjectFilesUpdate,
    ProjectMembershipUpdate,
)
from app.auth import CSRF_COOKIE, SESSION_COOKIE, SessionStore
from app.auth.authorization import can_access_workspace, project_actions, require_workspace
from app.core.errors import PublicError
from app.core.observability import CorrelationMiddleware
from app.core.security import ReplayGuard, consume_proxy_nonce, verify_csrf, verify_proxy_request
from app.core.studio_config import StudioConfig, load_studio_config, map_identity
from app.projects import registry, scaffold
from app.projects.runtime import validate_runtime_path
from app.projects.safe_fs import stable_file_target, walk_regular_files_nofollow
from app.projects.validation import InvalidProjectNameError, validate_project_file_path, validate_slug
from app.projects.workspace_recovery import require_workspace_ready, workspace_lock
from app.submissions.bootstrap import bootstrap_projects
from app.submissions.database import connect, transaction
from app.submissions.idempotency import canonical_request_hash, complete as complete_idempotency, reserve as reserve_idempotency
from app.submissions.logs import LogExportDispatcher
from app.submissions.migrations import migrate
from app.submissions.outbox import OutboxDispatcher, notification_channels
from app.submissions.provider_factory import create_providers
from app.submissions.recovery import recover_interrupted_submissions
from app.submissions.repository import SubmissionRepository
from app.submissions.repository_v2 import now_iso
from app.submissions.runtime_refs import normalize_runtime_references
from app.submissions.saga import SubmissionSaga
from app.submissions.snapshot import _open_regular_nofollow
from app.wsgi_bridge import create_wsgi_bridge

_PUBLIC_PATHS = {"/api/health", "/api/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
_PROXY_HEADERS = {
    "x-studio-identity", "x-studio-roles", "x-studio-timestamp", "x-studio-nonce",
    "x-studio-request-id", "x-studio-signature",
}
_ALLOWED_ROLES = {"admin", "reviewer", "submitter"}
_SAFE_RUNTIME_RESPONSE_TYPES = {"application/json", "text/plain"}
_OUTBOX_BATCH_LIMIT = 10
_OUTBOX_BATCH_TIMEOUT_SECONDS = 60.0
_OUTBOX_INTERVAL_SECONDS = 1.0
_RECOVERY_INTERVAL_SECONDS = 30.0
logger = logging.getLogger("toolbox_studio.asgi")


def _error_response(request: Request, code: str, message: str, status_code: int, *, recoverable: bool = False, field: str | None = None) -> JSONResponse:
    payload: dict[str, object] = {
        "error": code,
        "message": message,
        "recoverable": recoverable,
        "correlation_id": str(getattr(request.state, "correlation_id", "unknown")),
    }
    if field:
        payload["field"] = field
    return JSONResponse(payload, status_code=status_code)


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int = 2 * 1024 * 1024,
        editor_limit: int = 12 * 1024 * 1024,
        manifest_upload_limit: int = 12 * 1024 * 1024,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.editor_limit = editor_limit
        self.manifest_upload_limit = manifest_upload_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path == "/api/manifest/upload":
            limit = self.manifest_upload_limit
        elif path.endswith("/files") and path.startswith("/api/projects/"):
            limit = self.editor_limit
        else:
            limit = self.default_limit
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0") or b"0")
        except ValueError:
            declared = limit + 1
        if declared > limit:
            await self._reject(scope, send, limit)
            return
        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > limit:
                await self._reject(scope, send, limit)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(scope: Scope, send: Send, limit: int) -> None:
        correlation_id = str(scope.get("state", {}).get("correlation_id", "unknown"))
        body = json.dumps({
            "error": "request_too_large",
            "message": f"request body exceeds {limit} bytes",
            "recoverable": False,
            "correlation_id": correlation_id,
        }, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})


class AuthenticationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, config: StudioConfig, sessions: SessionStore) -> None:
        super().__init__(app)
        self.config = config
        self.sessions = sessions
        self.replay_guard = ReplayGuard()

    async def dispatch(self, request: Request, call_next):
        host = request.client.host if request.client else ""
        if request.method in {"POST", "PUT", "PATCH"} and "/app/" not in request.url.path:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            has_body = request.headers.get("content-length", "0") not in {"", "0"} or "transfer-encoding" in request.headers
            allowed_types = {"application/json"}
            if request.url.path == "/api/manifest/upload":
                allowed_types.add("multipart/form-data")
            if has_body and content_type not in allowed_types:
                return _error_response(request, "unsupported_media_type", "request content type is not allowed for this endpoint", 415)
        if self.config.is_local:
            if host not in _LOOPBACK:
                return _error_response(request, "loopback_required", "local profile requires a loopback client", 403)
            session = self.sessions.get(request.cookies.get(SESSION_COOKIE))
            if request.url.path == "/api/session" and request.method == "GET":
                identity = session.identity if session else self.config.local_user_id
            elif request.url.path in _PUBLIC_PATHS:
                request.state.identity = None
                request.state.roles = set()
                return await call_next(request)
            elif session is None:
                return _error_response(request, "session_required", "a local session is required", 401)
            else:
                identity = session.identity
            roles = set(self.config.identity_roles.get(identity, ()))
            if request.method in {"POST", "PUT", "PATCH"} and request.url.path != "/api/session":
                if not verify_csrf(request.cookies.get(CSRF_COOKIE), request.headers.get("x-csrf-token")):
                    return _error_response(request, "csrf_failed", "CSRF validation failed", 403)
        else:
            if request.url.path in _PUBLIC_PATHS:
                request.state.identity = None
                request.state.roles = set()
                return await call_next(request)
            if not self.config.peer_is_trusted(host):
                return _error_response(request, "untrusted_proxy_peer", "trusted proxy peer required", 401)
            raw_names = [name.decode("latin-1").lower() for name, _ in request.scope.get("headers", [])]
            if any(raw_names.count(name) != 1 for name in _PROXY_HEADERS):
                return _error_response(request, "invalid_proxy_headers", "proxy authentication headers are invalid", 401)
            headers = request.headers
            external = headers["x-studio-identity"]
            try:
                mapped_identity = map_identity(self.config, external)
            except ValueError:
                return _error_response(request, "invalid_proxy_identity", "proxy identity is invalid", 401)
            role_text = ",".join(sorted(filter(None, (item.strip() for item in headers["x-studio-roles"].split(",")))))
            roles = set(role_text.split(",")) if role_text else set()
            if not roles <= _ALLOWED_ROLES:
                return _error_response(request, "invalid_proxy_roles", "proxy roles are invalid", 401)
            body = await request.body()
            secret = self.config.proxy_hmac_secret or ""
            database_path = self.config.database_path

            def consume(nonce, actor, request_id, issued, ttl):
                connection = connect(database_path)
                try:
                    return consume_proxy_nonce(connection, nonce=nonce, actor=actor, request_id=request_id, timestamp=issued, ttl_seconds=ttl)
                finally:
                    connection.close()

            authenticated = verify_proxy_request(
                secret=secret,
                identity=external,
                roles=role_text,
                timestamp=headers["x-studio-timestamp"],
                nonce=headers["x-studio-nonce"],
                request_id=headers["x-studio-request-id"],
                signature=headers["x-studio-signature"],
                method=request.method,
                path=request.scope.get("raw_path", request.url.path.encode("utf-8")).decode("latin-1"),
                query=request.scope.get("query_string", b"").decode("latin-1"),
                body_sha256=hashlib.sha256(body).hexdigest(),
                nonce_consumer=consume,
            )
            if not authenticated:
                return _error_response(request, "unauthorized", "proxy authentication failed", 401)
            if request.method in {"POST", "PUT", "PATCH"} and request.headers.get("origin") not in self.config.cors_origins:
                return _error_response(request, "origin_forbidden", "request origin is not allowed", 403)
            identity = mapped_identity

        if request.url.path not in _PUBLIC_PATHS and not identity:
            return _error_response(request, "unauthorized", "proxy authentication failed", 401)
        request.state.identity = identity
        request.state.display_name = self.config.local_display_name if self.config.is_local else identity
        request.state.roles = roles
        response = await call_next(request)
        if self.config.is_local and request.url.path == "/api/session" and identity and session is None:
            session_id, created = self.sessions.create(identity)
            response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict", secure=False)
            response.set_cookie(CSRF_COOKIE, created.csrf_token, httponly=False, samesite="strict", secure=False)
        return response


def _project_meta(database_path: Path, slug: str) -> dict[str, object]:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT slug,display_name,created_at,modified_at,status FROM projects WHERE slug=?",
            (slug,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise PublicError("not_found", "project not found", 404)
    return {
        "name": row["slug"],
        "displayName": row["display_name"],
        "created_at": row["created_at"],
        "modified_at": row["modified_at"],
        "status": row["status"],
    }


def _project_files(slug: str) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    max_editable_bytes = PROJECT_FILE_MAX_BYTES
    for base, label in ((registry.frontend_project_dir(slug), "frontend"), (registry.backend_project_dir(slug), "backend")):
        for relative, source in walk_regular_files_nofollow(base):
            if label == "frontend" and relative == registry.META_FILENAME:
                continue
            digest = hashlib.sha256()
            payload = bytearray()
            size = 0
            with source.open() as (handle, _opened):
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    if len(payload) <= max_editable_bytes:
                        payload.extend(chunk)
            item: dict[str, object] = {
                "path": f"{label}/{relative}",
                "type": "opaque",
                "size": size,
                "sha256": digest.hexdigest(),
            }
            if size <= max_editable_bytes:
                try:
                    content = bytes(payload).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                    item["content"] = content
                    item["type"] = "editable"
                    item["sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                except UnicodeDecodeError:
                    pass
            dto = EditableFile(**item) if item["type"] == "editable" else OpaqueFile(**item)
            files.append(dto.model_dump())
    return files


def _file_sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with _open_regular_nofollow(path) as (handle, _opened):
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def _persist_workspace_save_state(
    database_path: Path,
    journal_id: str,
    manifest_bytes: bytes,
    slug: str,
    files: list[dict[str, str]],
    status: str,
    error_code: str | None = None,
) -> None:
    connection = connect(database_path)
    try:
        with transaction(connection):
            now = now_iso()
            connection.execute(
                "INSERT INTO workspace_save_recovery(journal_id,project_slug,status,manifest_sha256,error_code,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(journal_id) DO UPDATE SET "
                "status=excluded.status,manifest_sha256=excluded.manifest_sha256,error_code=excluded.error_code,updated_at=excluded.updated_at",
                (journal_id, slug, status, hashlib.sha256(manifest_bytes).hexdigest(), error_code, now, now),
            )
            if status == "completed":
                connection.execute("UPDATE projects SET modified_at=? WHERE slug=?", (now, slug))
            for index, item in enumerate(files):
                connection.execute(
                    "INSERT INTO workspace_save_items(journal_id,item_index,canonical_path,expected_sha256,status,updated_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(journal_id,item_index) DO UPDATE SET "
                    "canonical_path=excluded.canonical_path,expected_sha256=excluded.expected_sha256,updated_at=excluded.updated_at",
                    (journal_id, index, item["path"], item["expected_sha256"], "prepared", now),
                )
    finally:
        connection.close()


def _mark_workspace_save_item(database_path: Path, journal_id: str, index: int) -> None:
    connection = connect(database_path)
    try:
        with transaction(connection):
            connection.execute(
                "UPDATE workspace_save_items SET status='promoted',updated_at=? WHERE journal_id=? AND item_index=?",
                (now_iso(), journal_id, index),
            )
    finally:
        connection.close()


def _recover_project_save_journals_unlocked(database_path: Path, journal_id: str | None = None) -> list[dict[str, str]]:
    journal_root = database_path.parent / "workspace-journals"
    if not journal_root.is_dir():
        return []
    diagnostics: list[dict[str, str]] = []
    journals = [journal_root / journal_id] if journal_id else sorted(journal_root.iterdir())
    for journal in journals:
        manifest_path = journal / "manifest.json"
        if not journal.is_dir() or not manifest_path.is_file():
            continue
        if (journal / "completed").is_file():
            completed_connection = connect(database_path)
            try:
                completed_row = completed_connection.execute(
                    "SELECT status FROM workspace_save_recovery WHERE journal_id=?",
                    (journal.name,),
                ).fetchone()
            finally:
                completed_connection.close()
            if completed_row is not None and completed_row["status"] == "completed":
                continue
        slug: str | None = None
        files: list[dict[str, str]] = []
        guards = []
        manifest_bytes = b""
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("version") != 2 or not isinstance(manifest.get("files"), list) or not 1 <= len(manifest["files"]) <= 100:
                raise ValueError("invalid workspace journal")
            slug = validate_slug(str(manifest["slug"]))
            seen: set[str] = set()
            with ExitStack() as path_guards:
                for index, item in enumerate(manifest["files"]):
                    if not isinstance(item, dict):
                        raise ValueError("invalid workspace journal item")
                    prefix, child, collision_key = validate_project_file_path(f"{item.get('prefix', '')}/{item.get('child', '')}")
                    if collision_key in seen:
                        raise ValueError("duplicate workspace journal path")
                    seen.add(collision_key)
                    base = registry.frontend_project_dir(slug) if prefix == "frontend" else registry.backend_project_dir(slug)
                    guard = path_guards.enter_context(stable_file_target(base, child, create_parents=True))
                    guards.append(guard)
                    staged = journal / f"{index}.content"
                    expected = item.get("expected_sha256")
                    if not isinstance(expected, str) or len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
                        raise ValueError("workspace journal hash is invalid")
                    if _file_sha256(staged) != expected and guard.sha256() != expected:
                        raise ValueError("workspace journal content is unavailable")
                    files.append({"prefix": prefix, "child": child, "path": f"{prefix}/{child}", "expected_sha256": expected})
                normalized_manifest = {"version": 2, "slug": slug, "files": files}
                manifest_bytes = json.dumps(normalized_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
                _persist_workspace_save_state(database_path, journal.name, manifest_bytes, slug, files, "recovering")
                for index, (item, guard) in enumerate(zip(files, guards, strict=True)):
                    staged = journal / f"{index}.content"
                    if guard.sha256() != item["expected_sha256"]:
                        if _file_sha256(staged) != item["expected_sha256"]:
                            raise ValueError("workspace journal hash mismatch")
                        guard.replace_from(staged)
                        if guard.sha256() != item["expected_sha256"]:
                            raise ValueError("workspace promotion verification failed")
                    _mark_workspace_save_item(database_path, journal.name, index)
                completed = journal / "completed"
                with completed.open("w", encoding="utf-8") as handle:
                    handle.write(now_iso())
                    handle.flush()
                    os.fsync(handle.fileno())
                _persist_workspace_save_state(database_path, journal.name, manifest_bytes, slug, files, "completed")
        except (KeyError, TypeError, ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError, sqlite3.DatabaseError):
            if slug is not None and files and manifest_bytes:
                try:
                    _persist_workspace_save_state(
                        database_path, journal.name, manifest_bytes, slug, files,
                        "failed", "workspace_recovery_failed",
                    )
                except (OSError, sqlite3.DatabaseError):
                    pass
            diagnostics.append({"journal_id": journal.name, "project_slug": slug or "unknown", "error_code": "workspace_recovery_failed"})
    return diagnostics


def _recover_project_save_journals(database_path: Path, journal_id: str | None = None) -> list[dict[str, str]]:
    if journal_id is not None:
        return _recover_project_save_journals_unlocked(database_path, journal_id)
    journal_root = database_path.parent / "workspace-journals"
    if not journal_root.is_dir():
        return []
    diagnostics: list[dict[str, str]] = []
    for journal in sorted(journal_root.iterdir()):
        manifest_path = journal / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            slug = validate_slug(str(manifest["slug"]))
        except (KeyError, TypeError, ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            diagnostics.extend(_recover_project_save_journals_unlocked(database_path, journal.name))
            continue
        try:
            with workspace_lock(database_path, slug), registry.stable_project_roots(slug):
                diagnostics.extend(_recover_project_save_journals_unlocked(database_path, journal.name))
        except OSError:
            diagnostics.append({"journal_id": journal.name, "project_slug": slug, "error_code": "workspace_root_unsafe"})
    return diagnostics


def _workspace_recovery_diagnostics(database_path: Path) -> list[dict[str, str]]:
    connection = connect(database_path)
    try:
        return [
            {"journal_id": str(row["journal_id"]), "project_slug": str(row["project_slug"]), "error_code": str(row["error_code"] or "workspace_recovering")}
            for row in connection.execute(
                "SELECT journal_id,project_slug,error_code FROM workspace_save_recovery WHERE status!='completed' ORDER BY updated_at"
            ).fetchall()
        ]
    finally:
        connection.close()


def _save_project_files_unlocked(
    database_path: Path,
    slug: str,
    body: ProjectFilesUpdate,
    *,
    max_batch_bytes: int = PROJECT_BATCH_MAX_BYTES,
) -> None:
    encoded_contents: list[bytes] = []
    for item in body.files:
        encoded = item.content.encode("utf-8")
        if len(encoded) > PROJECT_FILE_MAX_BYTES:
            raise PublicError("file_too_large", "project file exceeds 2 MiB", 413, field="content")
        encoded_contents.append(encoded)
    if sum(map(len, encoded_contents)) > min(PROJECT_BATCH_MAX_BYTES, max_batch_bytes):
        raise PublicError("files_too_large", "project update exceeds 10 MiB", 413)
    require_workspace_ready(database_path, slug)
    resolved = []
    seen: set[str] = set()
    with ExitStack() as path_guards:
        for item, encoded in zip(body.files, encoded_contents, strict=True):
            try:
                prefix, child, collision_key = validate_project_file_path(item.path)
            except ValueError as exc:
                raise PublicError("invalid_project_path", "project file path is not canonical", 422, field="path") from exc
            if collision_key in seen:
                raise PublicError("duplicate_project_path", "project file paths collide canonically", 422, field="path")
            seen.add(collision_key)
            base = registry.frontend_project_dir(slug) if prefix == "frontend" else registry.backend_project_dir(slug)
            try:
                guard = path_guards.enter_context(stable_file_target(base, child, create_parents=True))
            except (OSError, ValueError) as exc:
                raise PublicError("invalid_project_path", "project file path is outside the project", 422, field="path") from exc
            current_sha = guard.sha256(normalize_text=True)
            if current_sha != item.base_sha256:
                raise PublicError("stale_project_file", f"project file changed: {item.path}", 409)
            resolved.append((guard, item.content, prefix, child, hashlib.sha256(encoded).hexdigest()))
        journal = database_path.parent / "workspace-journals" / ("save_" + secrets.token_hex(16))
        journal.mkdir(parents=True, exist_ok=False)
        files = [
            {"prefix": prefix, "child": child, "path": f"{prefix}/{child}", "expected_sha256": expected}
            for _guard, _content, prefix, child, expected in resolved
        ]
        manifest = {"version": 2, "slug": slug, "files": files}
        for index, (_guard, content, _prefix, _child, _expected) in enumerate(resolved):
            staged = journal / f"{index}.content"
            with staged.open("xb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with (journal / "manifest.json").open("xb") as handle:
            handle.write(manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        _persist_workspace_save_state(database_path, journal.name, manifest_bytes, slug, files, "recovering")
        _recover_project_save_journals(database_path, journal.name)
        require_workspace_ready(database_path, slug)


def _save_project_files(
    database_path: Path,
    slug: str,
    body: ProjectFilesUpdate,
    *,
    max_batch_bytes: int = PROJECT_BATCH_MAX_BYTES,
) -> None:
    try:
        with workspace_lock(database_path, slug), registry.stable_project_roots(slug):
            _save_project_files_unlocked(database_path, slug, body, max_batch_bytes=max_batch_bytes)
    except PublicError:
        raise
    except OSError as exc:
        raise PublicError("workspace_root_unsafe", "project workspace root is unsafe or changed", 409) from exc


def _project_creation_journal_root(database_path: Path) -> Path:
    return database_path.parent / "project-create-journals"


def _persist_project_creation_items(database_path: Path, manifest: dict[str, object]) -> list[dict[str, object]]:
    items = scaffold._validated_creation_items(manifest)
    connection = connect(database_path)
    try:
        with transaction(connection):
            for item in items:
                existing = connection.execute(
                    "SELECT item_kind,staging_name,target_name,expected_sha256,status FROM project_create_items "
                    "WHERE operation_id=? AND item_kind=?",
                    (manifest["operation_id"], item["root"]),
                ).fetchone()
                values = {
                    "item_kind": str(item["root"]),
                    "staging_name": str(item["staging_name"]),
                    "target_name": str(item["target_name"]),
                    "expected_sha256": str(item["sha256"]),
                }
                if existing is not None:
                    if any(str(existing[key]) != value for key, value in values.items()):
                        raise ValueError("project creation journal differs from durable items")
                    continue
                connection.execute(
                    "INSERT INTO project_create_items(operation_id,item_kind,staging_name,target_name,expected_sha256,status,updated_at) "
                    "VALUES (?,?,?,?,?,'prepared',?)",
                    (
                        manifest["operation_id"], values["item_kind"], values["staging_name"],
                        values["target_name"], values["expected_sha256"], now_iso(),
                    ),
                )
            rows = connection.execute(
                "SELECT item_kind,staging_name,target_name,expected_sha256,status FROM project_create_items "
                "WHERE operation_id=? ORDER BY item_kind",
                (manifest["operation_id"],),
            ).fetchall()
            durable = [dict(row) for row in rows]
            scaffold._validated_creation_items(manifest, durable)
            return durable
    finally:
        connection.close()


def _project_creation_items(database_path: Path, operation_id: str) -> list[dict[str, object]]:
    connection = connect(database_path)
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT item_kind,staging_name,target_name,expected_sha256,status FROM project_create_items "
                "WHERE operation_id=? ORDER BY item_kind",
                (operation_id,),
            ).fetchall()
        ]
    finally:
        connection.close()


def _complete_project_creation(database_path: Path, project: dict[str, object], actor: str, operation_id: str, manifest: dict[str, object] | None = None) -> None:
    connection = connect(database_path)
    try:
        with transaction(connection):
            SubmissionRepository(connection).upsert_project(
                slug=str(project["name"]),
                display_name=str(project.get("displayName") or project["name"]),
                created_at=project.get("created_at"),
                modified_at=project.get("modified_at"),
                status=str(project.get("status", "draft")),
            )
            connection.execute("UPDATE projects SET owner_id=COALESCE(owner_id,?) WHERE slug=?", (actor, project["name"]))
            connection.execute(
                "UPDATE project_create_reservations SET status='completed',updated_at=? WHERE slug=? AND operation_id=?",
                (now_iso(), project["name"], operation_id),
            )
            if manifest:
                for item in manifest["items"]:
                    connection.execute(
                        "UPDATE project_create_items SET status=?,updated_at=? WHERE operation_id=? AND item_kind=?",
                        (item.get("status", "promoted"), now_iso(), operation_id, item["root"]),
                    )
            project_response = dict(project)
            project_response["actions_allowed"] = ["project.read", "project.edit", "project.preview", "submission.create"]
            complete_idempotency(connection, operation_id, 201, project_response)
    finally:
        connection.close()


def _recover_project_creations(database_path: Path) -> list[dict[str, str]]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT slug,operation_id,actor_id,status,display_name FROM project_create_reservations "
            "WHERE status IN ('reserved','diagnostic') ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    diagnostics: list[dict[str, str]] = []
    journal_root = _project_creation_journal_root(database_path)
    for row in rows:
        if row["status"] == "diagnostic":
            diagnostics.append({"slug": row["slug"], "operation_id": row["operation_id"], "status": "diagnostic"})
            continue
        try:
            expected_items = _project_creation_items(database_path, str(row["operation_id"]))
            recovered = scaffold.recover_project_creation(
                str(row["operation_id"]), journal_root, expected_items,
            )
            if recovered is None:
                project = scaffold.create_project(
                    str(row["display_name"] or row["slug"]),
                    operation_id=str(row["operation_id"]),
                    journal_root=journal_root,
                    prepared_callback=lambda manifest: _persist_project_creation_items(database_path, manifest),
                )
                expected_items = _project_creation_items(database_path, str(row["operation_id"]))
                recovered = scaffold.recover_project_creation(
                    str(row["operation_id"]), journal_root, expected_items,
                )
                manifest = recovered[1] if recovered else None
            else:
                project, manifest = recovered
                _persist_project_creation_items(database_path, manifest)
            _complete_project_creation(database_path, project, str(row["actor_id"]), str(row["operation_id"]), manifest)
        except Exception:
            failed = connect(database_path)
            try:
                with transaction(failed):
                    failed.execute(
                        "UPDATE project_create_reservations SET status='diagnostic',updated_at=? WHERE operation_id=?",
                        (now_iso(), row["operation_id"]),
                    )
            finally:
                failed.close()
            diagnostics.append({"slug": row["slug"], "operation_id": row["operation_id"], "status": "diagnostic"})
    return diagnostics


def _api_router(database_path: Path, runtime_provider) -> APIRouter:
    router = APIRouter(prefix="/api")

    def can_create_project(request: Request, roles: set[str]) -> bool:
        if request.app.state.config.is_local:
            return bool(roles & {"submitter", "admin"})
        return "admin" in roles

    @router.get("/health")
    def health():
        return {"status": "ok", "profile": "", "station_codes": [], "station_code": "", "station_country": "ES"}

    def ready_payload(request: Request, *, detailed: bool) -> tuple[dict[str, object], int]:
        payload: dict[str, object] = {
            "status": "ready" if getattr(request.app.state, "ready", False) else "starting",
        }
        diagnostics = list(getattr(request.app.state, "project_creation_diagnostics", []))
        if diagnostics:
            payload["project_creation_diagnostics"] = diagnostics
        bootstrap_diagnostics = list(getattr(request.app.state, "project_bootstrap_diagnostics", []))
        if bootstrap_diagnostics:
            payload["project_bootstrap_diagnostics"] = bootstrap_diagnostics
            payload["status"] = "degraded"
        preview_recovery = list(getattr(request.app.state, "preview_recovery_operations", []))
        if preview_recovery:
            payload["preview_recovery_operations"] = preview_recovery
        recovery = getattr(request.app.state, "submission_recovery_status", {"status": "pending"})
        workspace_recovery = list(getattr(request.app.state, "workspace_recovery_diagnostics", []))
        known_journals = {item.get("journal_id") for item in workspace_recovery}
        workspace_recovery.extend(
            item for item in _workspace_recovery_diagnostics(database_path)
            if item.get("journal_id") not in known_journals
        )
        status_code = 200
        if workspace_recovery:
            payload["status"] = "degraded"
            payload["workspace_recovery"] = workspace_recovery
            status_code = 503
        if recovery.get("status") in {"pending", "failed"}:
            payload["status"] = "starting" if recovery.get("status") == "pending" else "degraded"
            payload["submission_recovery"] = {
                "status": recovery.get("status"),
                "error_code": recovery.get("error_code") or ("submission_recovery_pending" if recovery.get("status") == "pending" else "submission_recovery_failed"),
            }
            status_code = 503
        if not request.app.state.config.is_local and not detailed:
            safe: dict[str, object] = {"status": payload["status"]}
            for key in (
                "project_creation_diagnostics",
                "project_bootstrap_diagnostics",
                "workspace_recovery",
            ):
                items = payload.get(key)
                if not isinstance(items, list) or not items:
                    continue
                codes = sorted({
                    str(item.get("error_code") or item.get("status") or "diagnostic")
                    for item in items if isinstance(item, dict)
                })
                safe[key] = {"count": len(items), "codes": codes}
            if preview_recovery:
                safe["preview_recovery_operations"] = {
                    "count": len(preview_recovery),
                    "codes": ["preview_recovery_pending"],
                }
            if "submission_recovery" in payload:
                safe["submission_recovery"] = payload["submission_recovery"]
            payload = safe
        elif detailed:
            allowed_fields = {
                "project_creation_diagnostics": ("slug", "operation_id", "status"),
                "project_bootstrap_diagnostics": ("name", "status", "first_seen_at", "last_seen_at"),
                "workspace_recovery": ("journal_id", "project_slug", "error_code"),
            }
            for key, fields in allowed_fields.items():
                items = payload.get(key)
                if isinstance(items, list):
                    payload[key] = [
                        {field: item[field] for field in fields if field in item}
                        for item in items if isinstance(item, dict)
                    ]
        return payload, status_code

    @router.get("/ready")
    def ready(request: Request):
        payload, status_code = ready_payload(request, detailed=False)
        return JSONResponse(payload, status_code=status_code) if status_code != 200 else payload

    @router.get("/ready/details")
    def ready_details(request: Request):
        actor, roles = actor_roles(request)
        if "admin" not in roles:
            raise PublicError("forbidden", "admin diagnostics capability required", 403)
        payload, status_code = ready_payload(request, detailed=True)
        connection = connect(database_path)
        try:
            with transaction(connection):
                connection.execute(
                    "INSERT INTO admin_audit_events(event_type,actor_id,correlation_id,data_json,created_at) VALUES (?,?,?,?,?)",
                    (
                        "readiness_details_viewed",
                        actor,
                        str(getattr(request.state, "correlation_id", "")),
                        json.dumps({"status": payload["status"]}, sort_keys=True, separators=(",", ":")),
                        now_iso(),
                    ),
                )
        finally:
            connection.close()
        return JSONResponse(payload, status_code=status_code) if status_code != 200 else payload

    @router.get("/session")
    def session(request: Request):
        actor, roles = actor_roles(request)
        capabilities = ["project.create"] if can_create_project(request, roles) else []
        if roles & {"submitter", "admin"}:
            capabilities.append("submission.create")
        return {
            "identity": actor,
            "display_name": str(getattr(request.state, "display_name", actor)),
            "roles": sorted(roles),
            "capabilities": capabilities,
            "profile": request.app.state.config.profile,
        }

    @router.get("/config/owners")
    def owners():
        from app.core.owner_config import notification_targets, owners as owner_names
        targets = notification_targets()
        return {"owners": owner_names(), "email_enabled": bool(targets["emails"]), "slack_enabled": bool(targets["slack_webhook"])}

    @router.get("/projects")
    def list_projects(request: Request):
        actor, roles = actor_roles(request)
        connection = connect(database_path)
        try:
            visible = [item for item in registry.list_projects() if can_access_workspace(connection, str(item["name"]), actor, roles)]
            rows = {
                row["slug"]: row
                for row in connection.execute(
                    "SELECT slug,display_name,created_at,modified_at,status FROM projects"
                ).fetchall()
            }
            return [
                {
                    "name": item["name"],
                    "displayName": rows[item["name"]]["display_name"],
                    "created_at": rows[item["name"]]["created_at"],
                    "modified_at": rows[item["name"]]["modified_at"],
                    "status": rows[item["name"]]["status"],
                }
                if item["name"] in rows else item
                for item in visible
            ]
        finally:
            connection.close()

    @router.post("/projects", status_code=201)
    def create_project(
        body: ProjectCreate,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        actor, roles = actor_roles(request)
        if not can_create_project(request, roles):
            raise PublicError("forbidden", "project.create capability required", 403)
        if idempotency_key is None:
            raise PublicError("idempotency_key_required", "Idempotency-Key is required", 422)
        raw_name = body.name
        try:
            slug = scaffold.slugify(raw_name)
        except (AttributeError, scaffold.InvalidProjectNameError) as exc:
            raise PublicError("invalid_name", str(exc), 400) from exc
        operation_id = "op_" + secrets.token_hex(16)
        connection = connect(database_path)
        try:
            with transaction(connection):
                idem = reserve_idempotency(
                    connection,
                    scope="project",
                    actor_id=actor,
                    route_template="/api/projects",
                    resource_id=slug,
                    key=idempotency_key,
                    request_sha256=canonical_request_hash({"name": raw_name}),
                    operation_id=operation_id,
                )
                if idem.kind != "new":
                    if idem.response is not None:
                        response.status_code = int(idem.status_code or 201)
                        return idem.response
                    response.status_code = 202
                    return {"operation_id": idem.operation_id, "name": slug, "status": "creating"}
                operation_id = idem.operation_id
                if registry.project_exists(slug) or connection.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                    raise PublicError("name_exists", f"Project '{slug}' already exists.", 409)
                now = now_iso()
                try:
                    connection.execute(
                        "INSERT INTO project_create_reservations(slug,operation_id,actor_id,status,created_at,updated_at,display_name) VALUES (?,?,?,'reserved',?,?,?)",
                        (slug, operation_id, actor, now, now, raw_name),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PublicError("name_exists", f"Project '{slug}' is already being created.", 409) from exc
        finally:
            connection.close()
        try:
            project = scaffold.create_project(
                raw_name,
                operation_id=operation_id,
                journal_root=_project_creation_journal_root(database_path),
                prepared_callback=lambda manifest: _persist_project_creation_items(database_path, manifest),
            )
        except scaffold.InvalidProjectNameError as exc:
            raise PublicError("invalid_name", str(exc), 400) from exc
        except scaffold.ProjectAlreadyExistsError as exc:
            failed = connect(database_path)
            try:
                with transaction(failed):
                    failed.execute(
                        "UPDATE project_create_reservations SET status='diagnostic',updated_at=? WHERE slug=? AND operation_id=?",
                        (now_iso(), slug, operation_id),
                    )
            finally:
                failed.close()
            raise PublicError("project_diagnostic", str(exc), 409) from exc
        except Exception as exc:
            raise PublicError(
                "project_creation_recovering",
                "project creation is reserved for roll-forward recovery",
                503,
                recoverable=True,
            ) from exc
        expected_items = _project_creation_items(database_path, operation_id)
        recovered = scaffold.recover_project_creation(
            operation_id,
            _project_creation_journal_root(database_path),
            expected_items,
        )
        _complete_project_creation(database_path, project, actor, operation_id, recovered[1] if recovered else None)
        project["actions_allowed"] = ["project.read", "project.edit", "project.preview", "submission.create"]
        return project

    def authorize_project(slug: str, request: Request, capability: str):
        try:
            validate_slug(slug)
        except InvalidProjectNameError as exc:
            raise PublicError("invalid_project_slug", "invalid project slug", 422) from exc
        actor, roles = actor_roles(request)
        connection = connect(database_path)
        try:
            require_workspace(connection, slug, actor, roles, capability)
            return project_actions(connection, slug, actor, roles)
        finally:
            connection.close()

    @router.get("/projects/{slug}")
    def get_project(slug: str, request: Request):
        actions = authorize_project(slug, request, "project.read")
        try:
            with workspace_lock(database_path, slug), registry.stable_project_roots(slug):
                require_workspace_ready(database_path, slug)
                if not registry.project_exists(slug):
                    raise PublicError("not_found", "project not found", 404)
                return {"meta": _project_meta(database_path, slug), "files": _project_files(slug), "actions_allowed": actions}
        except PublicError:
            raise
        except OSError as exc:
            raise PublicError("workspace_root_unsafe", "project workspace root is unsafe or changed", 409) from exc

    @router.put("/projects/{slug}/files")
    def save_project(slug: str, body: ProjectFilesUpdate, request: Request):
        if not request.app.state.config.is_local:
            raise PublicError("feature_unavailable", "workspace editing is disabled in shared profile; use the isolated source workflow", 404)
        authorize_project(slug, request, "project.edit")
        require_workspace_ready(database_path, slug)
        if not registry.project_exists(slug):
            raise PublicError("not_found", "project not found", 404)
        _save_project_files(
            database_path,
            slug,
            body,
            max_batch_bytes=request.app.state.config.editor_body_limit_bytes,
        )
        return _project_meta(database_path, slug)

    @router.put("/projects/{slug}/members/{user_id}")
    def set_project_member(slug: str, user_id: str, body: ProjectMembershipUpdate, request: Request):
        actor, roles = actor_roles(request)
        if "admin" not in roles:
            raise PublicError("forbidden", "project.members capability required", 403)
        try:
            validate_slug(slug)
        except InvalidProjectNameError as exc:
            raise PublicError("invalid_project_slug", "invalid project slug", 422) from exc
        from app.core.studio_config import normalize_identity
        try:
            member_id = normalize_identity(user_id, "member identity")
        except ValueError as exc:
            raise PublicError("invalid_identity", "member identity is invalid", 422) from exc
        connection = connect(database_path)
        try:
            with transaction(connection):
                if connection.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone() is None:
                    raise PublicError("not_found", "project not found", 404)
                current = connection.execute("SELECT COALESCE(MAX(version),0) FROM project_memberships WHERE project_slug=? AND user_id=?", (slug, member_id)).fetchone()[0]
                if int(current) != body.expected_version:
                    raise PublicError("stale_membership", "project membership changed", 409)
                version = int(current) + 1
                now = now_iso()
                connection.execute("INSERT INTO project_memberships(project_slug,user_id,active,version,actor_id,created_at) VALUES (?,?,?,?,?,?)", (slug, member_id, int(body.active), version, actor, now))
                seq = int(connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM project_security_events WHERE project_slug=?", (slug,)).fetchone()[0])
                connection.execute("INSERT INTO project_security_events(project_slug,seq,event_type,actor_id,data_json,correlation_id,created_at) VALUES (?,?,?,?,?,?,?)", (slug, seq, "membership_changed", actor, json.dumps({"user_id": member_id, "active": body.active, "version": version}, sort_keys=True), getattr(request.state, "correlation_id", None), now))
                return {"project_slug": slug, "user_id": member_id, "active": body.active, "version": version}
        finally:
            connection.close()

    @router.post("/projects/{slug}/claim")
    def claim_project(slug: str, body: ProjectClaim, request: Request):
        actor, roles = actor_roles(request)
        if "admin" not in roles:
            raise PublicError("forbidden", "project.claim capability required", 403)
        try:
            validate_slug(slug)
        except InvalidProjectNameError as exc:
            raise PublicError("invalid_project_slug", "invalid project slug", 422) from exc
        connection = connect(database_path)
        try:
            with transaction(connection):
                row = connection.execute("SELECT owner_id,version FROM projects WHERE slug=?", (slug,)).fetchone()
                if row is None:
                    raise PublicError("not_found", "project not found", 404)
                if int(row["version"]) != body.expected_version or row["owner_id"] not in {None, actor}:
                    raise PublicError("stale_project", "project ownership changed", 409)
                cursor = connection.execute("UPDATE projects SET owner_id=?,version=version+1 WHERE slug=? AND version=? AND (owner_id IS NULL OR owner_id=?)", (actor, slug, body.expected_version, actor))
                if cursor.rowcount != 1:
                    raise PublicError("stale_project", "project ownership changed", 409)
                now = now_iso()
                seq = int(connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM project_security_events WHERE project_slug=?", (slug,)).fetchone()[0])
                connection.execute("INSERT INTO project_security_events(project_slug,seq,event_type,actor_id,data_json,correlation_id,created_at) VALUES (?,?,?,?,?,?,?)", (slug, seq, "project_claimed", actor, "{}", getattr(request.state, "correlation_id", None), now))
                return {"project_slug": slug, "owner_id": actor, "version": body.expected_version + 1}
        finally:
            connection.close()

    @router.api_route("/projects/{slug}/app/{runtime_path:path}", methods=["GET", "POST", "PUT", "PATCH"])
    async def project_runtime(slug: str, runtime_path: str, request: Request):
        authorize_project(slug, request, "project.runtime")
        raw_path = request.scope.get("raw_path", request.url.path.encode("utf-8")).decode("latin-1")
        runtime_path = validate_runtime_path(runtime_path, raw_path=raw_path)
        if not request.app.state.config.is_local and not getattr(runtime_provider, "isolated", False):
            raise PublicError("feature_unavailable", "local project runtime is disabled in shared profile", 404)
        body = await request.body()
        if len(body) > 2 * 1024 * 1024:
            raise PublicError("request_too_large", "runtime request exceeds 2 MiB", 413)
        def invoke_runtime():
            with workspace_lock(database_path, slug):
                require_workspace_ready(database_path, slug)
                return runtime_provider.invoke(
                    slug,
                    runtime_path,
                    request.method,
                    request.scope.get("query_string", b"").decode("latin-1"),
                    body,
                    request.headers.get("content-type"),
                )

        result = await run_in_threadpool(invoke_runtime)
        if len(result.body) > 10 * 1024 * 1024:
            raise PublicError("runtime_response_too_large", "runtime response exceeds 10 MiB", 502)
        media_type = result.content_type.split(";", 1)[0].strip().lower()
        headers = {
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        }
        if media_type not in _SAFE_RUNTIME_RESPONSE_TYPES:
            media_type = "application/octet-stream"
            headers["Content-Disposition"] = 'attachment; filename="project-runtime-response.bin"'
        return Response(result.body, status_code=result.status_code, media_type=media_type, headers=headers)

    return router


def create_app(config: StudioConfig | None = None, *, providers=None, provider_clients: dict[str, object] | None = None) -> FastAPI:
    effective = config or load_studio_config()
    effective.validate_runtime_safety()
    runtime_root = effective.database_path.parent
    configured_providers = providers or create_providers(
        effective, runtime_root,
        frontend_root=registry.FRONTEND_PROJECTS_DIR.parents[1],
        backend_root=registry.BACKEND_PROJECTS_DIR.parents[1],
        clients=provider_clients,
    )
    if not effective.is_local and (
        not getattr(configured_providers.build, "isolated", False)
        or not getattr(configured_providers.runtime, "isolated", False)
        or getattr(configured_providers.artifacts, "provider", None) != "s3"
    ):
        raise ValueError("config_invalid: shared provider contracts must be isolated and use S3 artifacts")
    if not effective.is_local and callable(getattr(configured_providers.build, "preflight", None)):
        build_preflight = configured_providers.build.preflight()
        if not build_preflight.available:
            raise ValueError("config_invalid: " + build_preflight.detail)
    saga = SubmissionSaga(
        effective.database_path,
        runtime_root,
        configured_providers,
        review_base_url=effective.review_base_url,
        preview_origin=effective.preview_origin,
        git_require_clean=effective.git_require_clean,
        file_exclusions=effective.file_exclusions,
        submit_wait_seconds=effective.submit_wait_seconds,
        build_timeout_seconds=effective.build_timeout_seconds,
        snapshot_max_files=effective.snapshot_max_files,
        snapshot_max_bytes=effective.snapshot_max_bytes,
        artifact_max_bytes=effective.artifact_max_bytes,
        max_builds_global=effective.max_builds_global,
        max_builds_per_project=effective.max_builds_per_project,
        storage_spool_max_bytes=effective.storage_spool_max_bytes,
        storage_min_free_bytes=effective.storage_min_free_bytes,
    )

    preview_router = create_preview_router(
        effective.database_path,
        runtime_root,
        effective,
        workspace_guard=require_workspace_ready,
        build_provider=configured_providers.build,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(effective.database_path)
        normalize_runtime_references(effective.database_path, runtime_root)
        app.state.project_creation_diagnostics = _recover_project_creations(effective.database_path)
        connection = connect(effective.database_path)
        try:
            bootstrap_projects(
                registry.FRONTEND_PROJECTS_DIR,
                registry.BACKEND_PROJECTS_DIR,
                SubmissionRepository(connection),
                local_identity=effective.local_user_id if effective.is_local else None,
            )
            app.state.project_bootstrap_diagnostics = [dict(row) for row in connection.execute(
                "SELECT name,status,first_seen_at,last_seen_at FROM project_bootstrap_diagnostics WHERE active=1 ORDER BY name"
            ).fetchall()]
            connection.execute(
                "INSERT INTO operational_status(key,status,error_code,updated_at) VALUES ('submission_recovery','pending',NULL,?) "
                "ON CONFLICT(key) DO UPDATE SET status='pending',error_code=NULL,updated_at=excluded.updated_at",
                (now_iso(),),
            )
            connection.commit()
            app.state.submission_recovery_status = {"status": "pending", "error_code": None}
        finally:
            connection.close()

        app.state.workspace_recovery_diagnostics = _recover_project_save_journals(effective.database_path)
        app.state.preview_recovery_operations = preview_router.preview_build_service.recover()
        stop_workers = asyncio.Event()

        def persist_recovery_status(status: str, error_code: str | None) -> None:
            worker_connection = connect(effective.database_path)
            try:
                with transaction(worker_connection):
                    worker_connection.execute(
                        "INSERT INTO operational_status(key,status,error_code,updated_at) VALUES ('submission_recovery',?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET status=excluded.status,error_code=excluded.error_code,updated_at=excluded.updated_at",
                        (status, error_code, now_iso()),
                    )
            finally:
                worker_connection.close()
            app.state.submission_recovery_status = {"status": status, "error_code": error_code}

        def recover_submissions() -> None:
            recover_interrupted_submissions(
                effective.database_path,
                configured_providers.artifacts,
                review_base_url=effective.review_base_url,
                notification_provider_keys=notification_channels(configured_providers),
                resume_callback=saga.run,
            )
            recovery_connection = connect(effective.database_path)
            try:
                unconverged = recovery_connection.execute(
                    "SELECT COUNT(*) FROM submission_records s LEFT JOIN build_jobs j ON j.submission_id=s.id "
                    "WHERE s.state='building' AND (j.status IS NULL OR j.status!='running' OR j.lease_until IS NULL OR j.lease_until<=?)",
                    (now_iso(),),
                ).fetchone()[0]
            finally:
                recovery_connection.close()
            if int(unconverged):
                raise RuntimeError("submission recovery did not converge")

        def dispatch_outbox_batch() -> None:
            worker_connection = connect(effective.database_path)
            try:
                OutboxDispatcher(worker_connection, configured_providers).dispatch_pending(limit=_OUTBOX_BATCH_LIMIT)
            finally:
                worker_connection.close()

        def dispatch_log_export_batch() -> None:
            exporter = getattr(configured_providers, "log_exporter", None)
            if exporter is not None:
                LogExportDispatcher(
                    effective.database_path,
                    configured_providers.logs,
                    exporter,
                    timeout_seconds=5,
                ).dispatch_pending(limit=_OUTBOX_BATCH_LIMIT)

        async def wait_for_worker_interval(seconds: float) -> None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_workers.wait(), timeout=seconds)

        async def dispatch_outbox() -> None:
            pending: asyncio.Task | None = None
            while not stop_workers.is_set():
                if pending is None:
                    pending = asyncio.create_task(asyncio.to_thread(dispatch_outbox_batch))
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=_OUTBOX_BATCH_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning("outbox batch exceeded its bounded wait", extra={"error_code": "outbox_batch_timeout"})
                except Exception:
                    logger.error("outbox batch failed", extra={"error_code": "outbox_dispatch_failed"})
                if pending.done():
                    pending = None
                await wait_for_worker_interval(_OUTBOX_INTERVAL_SECONDS)
            if pending is not None:
                with suppress(Exception):
                    await pending

        async def dispatch_log_exports() -> None:
            while not stop_workers.is_set():
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(dispatch_log_export_batch),
                        timeout=_OUTBOX_BATCH_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning("log export batch exceeded its bounded wait", extra={"error_code": "log_export_batch_timeout"})
                except Exception:
                    logger.error("log export batch failed", extra={"error_code": "log_export_failed"})
                await wait_for_worker_interval(_OUTBOX_INTERVAL_SECONDS)

        async def recover_submissions_periodically() -> None:
            while not stop_workers.is_set():
                try:
                    await asyncio.to_thread(recover_submissions)
                except Exception as exc:
                    error_code = str(getattr(exc, "code", "submission_recovery_failed"))
                    logger.error("submission recovery failed", extra={"error_code": error_code})
                    await asyncio.to_thread(persist_recovery_status, "failed", error_code)
                else:
                    await asyncio.to_thread(persist_recovery_status, "ok", None)
                await wait_for_worker_interval(_RECOVERY_INTERVAL_SECONDS)

        outbox_task = asyncio.create_task(dispatch_outbox())
        log_export_task = asyncio.create_task(dispatch_log_exports())
        recovery_task = asyncio.create_task(recover_submissions_periodically())
        app.state.outbox_task = outbox_task
        app.state.log_export_task = log_export_task
        app.state.submission_recovery_task = recovery_task
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            stop_workers.set()
            await asyncio.gather(outbox_task, log_export_task, recovery_task, return_exceptions=True)

    app = FastAPI(title="Toolbox Studio", version="1.0.0", lifespan=lifespan)
    app.state.config = effective
    app.state.ready = False
    app.state.project_creation_diagnostics = []
    app.state.project_bootstrap_diagnostics = []
    app.state.workspace_recovery_diagnostics = []
    app.state.preview_recovery_operations = []
    app.state.submission_recovery_status = {"status": "pending", "error_code": None}
    app.state.providers = configured_providers
    app.state.submission_saga = saga
    app.include_router(_api_router(effective.database_path, configured_providers.runtime))
    app.include_router(create_submissions_router(saga, configured_providers, workspace_guard=require_workspace_ready))
    app.include_router(create_review_router(effective.database_path, configured_providers, effective.review_base_url))
    app.include_router(preview_router)

    @app.exception_handler(PublicError)
    async def public_error_handler(request: Request, exc: PublicError):
        return JSONResponse(exc.as_dict(str(getattr(request.state, "correlation_id", "unknown"))), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        error_type = str(first.get("type", ""))
        code = "invalid_json" if error_type == "json_invalid" else "validation_error"
        field = ".".join(str(part) for part in first.get("loc", ())[1:]) or None
        message = "request body is not valid JSON" if code == "invalid_json" else "request validation failed"
        return _error_response(request, code, message, 422, field=field)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "method_not_allowed" if exc.status_code == 405 else "http_error"
        return _error_response(request, code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, _exc: Exception):
        return _error_response(request, "internal_error", "an internal error occurred", 500, recoverable=True)

    if effective.is_local:
        bridge = WSGIMiddleware(create_wsgi_bridge())

        class BridgeIdentityMiddleware:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    identity = scope.get("state", {}).get("identity")
                    if identity:
                        scope["headers"] = list(scope.get("headers", [])) + [(b"x-studio-authorized-identity", identity.encode("utf-8"))]
                await self.wrapped(scope, receive, send)

        app.mount("/", BridgeIdentityMiddleware(bridge))
    app.add_middleware(AuthenticationMiddleware, config=effective, sessions=SessionStore())
    app.add_middleware(
        BodyLimitMiddleware,
        default_limit=effective.request_body_limit_bytes,
        editor_limit=effective.editor_body_limit_bytes,
        manifest_upload_limit=effective.manifest_upload_limit_bytes,
    )
    app.add_middleware(CorrelationMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=list(effective.cors_origins), allow_credentials=True, allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH"], allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"])
    return app


def create_asgi_app(config: StudioConfig | None = None) -> FastAPI:
    return create_app(config)


app = create_app()
