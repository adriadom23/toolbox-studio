from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import PublicError
from app.core.observability import CorrelationMiddleware
from app.core.studio_config import DEFAULT_DATABASE_PATH, StudioConfig, load_studio_config
from app.projects.runtime import IsolatedGatewayRuntimeProvider, LocalProjectRuntimeProvider, validate_runtime_path
from app.projects.validation import InvalidProjectNameError, validate_slug
from app.submissions.database import connect, transaction
from app.submissions.migrations import migrate
from app.submissions.preview import PreviewManager
from app.submissions.repository_v2 import now_iso

logger = logging.getLogger("toolbox_studio.preview")
_ALLOWED_RUNTIME_CONTENT_TYPES = {"", "application/json", "application/x-www-form-urlencoded", "text/plain"}
_ALLOWED_RESPONSE_TYPES = {"application/json", "text/plain", "text/html", "application/octet-stream"}


class ExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nonce: str
    preview_id: str
    subject_kind: str
    subject_id: str


class PreviewBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, exchange_limit: int = 64 * 1024, runtime_limit: int = 2 * 1024 * 1024) -> None:
        self.app = app
        self.exchange_limit = exchange_limit
        self.runtime_limit = runtime_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        limit = self.exchange_limit if path == "/exchange" else self.runtime_limit if path.startswith("/api/projects/") and "/app/" in path else self.exchange_limit
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
        body = json.dumps({
            "error": "request_too_large",
            "message": f"request body exceeds {limit} bytes",
            "recoverable": False,
            "correlation_id": str(scope.get("state", {}).get("correlation_id", "unknown")),
        }, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, studio_origin: str) -> None:
        super().__init__(app)
        self.csp = f"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors {studio_origin}; base-uri 'none'; sandbox allow-scripts allow-same-origin"

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = self.csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


def _bootstrap_script(studio_origin: str) -> str:
    origin = json.dumps(studio_origin)
    return f"addEventListener('message',async(e)=>{{if(e.source!==parent||e.origin!=={origin}||!e.data||e.data.type!=='toolbox-preview-grant')return;const r=await fetch('/exchange',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify(e.data.grant)}});if(r.ok){{const d=await r.json();location.replace(d.url)}}}})"


def create_preview_app(*, config: StudioConfig | None = None, database_path: Path | None = None, preview_root: Path | None = None, secure_cookie: bool | None = None, runtime_factory=None) -> FastAPI:
    effective = config or load_studio_config()
    effective.validate_runtime_safety()
    database = Path(database_path or effective.database_path or DEFAULT_DATABASE_PATH)
    root = Path(preview_root or database.parent / "previews")
    preview_scheme = urlparse(effective.preview_origin).scheme.lower()
    if preview_scheme not in {"http", "https"}:
        raise ValueError("config_invalid: preview origin must use HTTP or HTTPS")
    expected_secure_cookie = preview_scheme == "https"
    if secure_cookie is not None and bool(secure_cookie) != expected_secure_cookie:
        raise ValueError("config_invalid: preview cookie security must match preview origin")
    secure_cookie = expected_secure_cookie
    cookie_name = "__Host-tb_preview" if secure_cookie else "tb_preview_local"
    studio_origin = effective.public_base_url
    preview_origin = effective.preview_origin
    if effective.is_local:
        make_runtime = runtime_factory or (
            lambda runtime_root: LocalProjectRuntimeProvider(runtime_root, timeout_seconds=30, workspace_guard=False)
        )
    else:
        if runtime_factory is not None:
            make_runtime = runtime_factory
        else:
            if not effective.runtime_gateway_url or not effective.runtime_gateway_token_env or not effective.runtime_gateway_allowed_hosts:
                raise ValueError("config_invalid: shared preview requires an allowlisted isolated runtime gateway")
            gateway = IsolatedGatewayRuntimeProvider(
                effective.runtime_gateway_url,
                effective.runtime_gateway_token_env,
                effective.runtime_gateway_allowed_hosts,
                timeout_seconds=30,
            )
            make_runtime = lambda _runtime_root: gateway

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(database)
        app.state.preview_manager = PreviewManager(database, root, preview_origin=preview_origin)
        app.state.runtime_slots = asyncio.Semaphore(4)
        yield

    app = FastAPI(title="Toolbox Studio Preview", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/bootstrap", response_class=HTMLResponse)
    def bootstrap():
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Toolbox Preview</title><script src='/bootstrap.js'></script><p>Preview no disponible</p>")

    @app.get("/bootstrap.js")
    def bootstrap_script():
        return HTMLResponse(_bootstrap_script(studio_origin), media_type="text/javascript")

    @app.post("/exchange")
    def exchange(body: ExchangeBody, request: Request):
        token = request.app.state.preview_manager.exchange(body.nonce, preview_id=body.preview_id, subject_kind=body.subject_kind, subject_id=body.subject_id)
        response = JSONResponse({"url": f"/preview/{body.preview_id}/index.html"})
        response.set_cookie(cookie_name, token, httponly=True, samesite="strict", secure=secure_cookie, path="/")
        return response

    @app.get("/preview/{preview_id}/{asset_path:path}")
    def asset(preview_id: str, asset_path: str, request: Request):
        token = request.cookies.get(cookie_name)
        manager = request.app.state.preview_manager
        row = manager.authorize(token or "", preview_id)
        if row is None:
            raise PublicError("preview_unauthorized", "preview session required", 403)
        opened = manager.open_asset(row, asset_path)
        handle, media, size = opened.__enter__()

        def content():
            try:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
            finally:
                opened.__exit__(None, None, None)

        return StreamingResponse(content(), media_type=media, headers={"Content-Length": str(size)})

    @app.api_route("/api/projects/{slug}/app/{runtime_path:path}", methods=["GET", "POST", "PUT", "PATCH"])
    async def runtime(slug: str, runtime_path: str, request: Request):
        try:
            validate_slug(slug)
        except InvalidProjectNameError as exc:
            raise PublicError("invalid_project_slug", "invalid project slug", 404) from exc
        raw_path = request.scope.get("raw_path", request.url.path.encode("utf-8")).decode("latin-1")
        runtime_path = validate_runtime_path(runtime_path, raw_path=raw_path)
        if len(request.scope.get("query_string", b"")) > 8192:
            raise PublicError("invalid_runtime_path", "runtime path is invalid", 422)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in _ALLOWED_RUNTIME_CONTENT_TYPES:
            raise PublicError("runtime_content_type_forbidden", "runtime content type is not allowed", 415)
        body = await request.body()
        if len(body) > 2 * 1024 * 1024:
            raise PublicError("request_too_large", "runtime request exceeds 2 MiB", 413)
        token = request.cookies.get(cookie_name, "")
        manager = request.app.state.preview_manager
        row = manager.authorize_runtime(token, slug)
        if row is None:
            raise PublicError("preview_runtime_unauthorized", "runtime is not authorized for this preview", 403)
        correlation_id = str(getattr(request.state, "correlation_id", ""))
        status_code = 502
        error_code = None
        try:
            async with request.app.state.runtime_slots:
                with manager.open_runtime(row) as runtime_root:
                    provider = make_runtime(runtime_root)
                    if not effective.is_local and not getattr(provider, "isolated", False):
                        raise PublicError("runtime_unavailable", "shared preview runtime requires an isolated gateway", 409)
                    result = await asyncio.wait_for(
                        run_in_threadpool(provider.invoke, slug, runtime_path, request.method, request.scope.get("query_string", b"").decode("latin-1"), body, content_type or None),
                        timeout=35,
                    )
            if 300 <= result.status_code < 400:
                raise PublicError("runtime_redirect_blocked", "runtime redirects are not allowed", 502)
            if len(result.body) > 10 * 1024 * 1024:
                raise PublicError("runtime_response_too_large", "runtime response exceeds 10 MiB", 502)
            media = result.content_type.split(";", 1)[0].lower()
            if media not in _ALLOWED_RESPONSE_TYPES:
                media = "application/octet-stream"
            status_code = result.status_code
            return StreamingResponse(iter((result.body,)), status_code=result.status_code, media_type=media)
        except asyncio.TimeoutError as exc:
            status_code = 504
            error_code = "runtime_timeout"
            raise PublicError(error_code, "project runtime timed out", status_code) from exc
        except PublicError as exc:
            status_code = exc.status_code
            error_code = exc.code
            raise
        except Exception:
            status_code = 500
            error_code = "internal_error"
            raise
        finally:
            connection = connect(database)
            try:
                with transaction(connection):
                    connection.execute(
                        "INSERT INTO preview_runtime_events(preview_id,actor_id,digest,method,runtime_path,status_code,correlation_id,created_at,error_code) VALUES (?,?,?,?,?,?,?,?,?)",
                        (row["id"], row["session_actor_id"], row["digest"], request.method, runtime_path, status_code, correlation_id, now_iso(), error_code),
                    )
            finally:
                connection.close()
            logger.info("preview runtime request", extra={"preview_id": row["id"], "digest": row["digest"], "method": request.method, "runtime_path": runtime_path, "status_code": status_code, "correlation_id": correlation_id})

    @app.exception_handler(PublicError)
    async def public_error(_request: Request, exc: PublicError):
        return JSONResponse(exc.as_dict(), status_code=exc.status_code)

    app.add_middleware(SecurityHeadersMiddleware, studio_origin=studio_origin)
    app.add_middleware(PreviewBodyLimitMiddleware, runtime_limit=effective.request_body_limit_bytes)
    app.add_middleware(CorrelationMiddleware)
    return app


app = create_preview_app()
