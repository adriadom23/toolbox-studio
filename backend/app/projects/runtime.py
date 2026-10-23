from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.errors import PublicError
from app.projects import registry
from app.projects.safe_fs import stable_directories
from app.projects.validation import validate_slug
from app.submissions.build import BuildFailed, _run, sanitized_environment

_RUNTIME_SLOTS = threading.BoundedSemaphore(4)
_RUNTIME_PROJECT_LOCK = threading.Lock()
_RUNTIME_PROJECT_SLOTS: dict[str, threading.BoundedSemaphore] = {}


def validate_runtime_path(value: str, *, raw_path: str | None = None) -> str:
    if (
        not value
        or len(value) > 500
        or "\\" in value
        or "%" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PublicError("invalid_runtime_path", "runtime path is invalid", 422)
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise PublicError("invalid_runtime_path", "runtime path is invalid", 422)
    path = PurePosixPath(value)
    if path.is_absolute():
        raise PublicError("invalid_runtime_path", "runtime path is invalid", 422)
    if raw_path is not None:
        raw_tail = raw_path.rsplit("/app/", 1)[-1]
        if "%" in raw_tail or "\\" in raw_tail:
            raise PublicError("invalid_runtime_path", "runtime path is invalid", 422)
    return path.as_posix()


@dataclass(frozen=True)
class RuntimeResponse:
    status_code: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class SealedRuntimeSource:
    path: Path
    inherited_fds: tuple[int, ...]


@contextmanager
def _runtime_slot(slug: str):
    with _RUNTIME_PROJECT_LOCK:
        project_slot = _RUNTIME_PROJECT_SLOTS.setdefault(slug, threading.BoundedSemaphore(1))
    if not _RUNTIME_SLOTS.acquire(timeout=2):
        raise PublicError("runtime_capacity_exceeded", "project runtime capacity is exhausted", 429, recoverable=True)
    try:
        if not project_slot.acquire(timeout=2):
            raise PublicError("runtime_project_busy", "project runtime is busy", 429, recoverable=True)
        try:
            yield
        finally:
            project_slot.release()
    finally:
        _RUNTIME_SLOTS.release()


class LocalProjectRuntimeProvider:
    isolated = False

    def __init__(self, backend_root: Path | SealedRuntimeSource, timeout_seconds: int = 30, *, workspace_guard: bool = True) -> None:
        self.sealed_source = backend_root if isinstance(backend_root, SealedRuntimeSource) else None
        self.backend_root = self.sealed_source.path if self.sealed_source else Path(backend_root)
        self.timeout_seconds = timeout_seconds
        self.workspace_guard = workspace_guard
        self.runner = Path(__file__).resolve().parents[1] / "submissions" / "runners" / "invoke_project_runtime.py"

    @contextmanager
    def _source_guard(self, slug: str):
        if self.sealed_source is not None:
            yield
            return
        if self.workspace_guard:
            with registry.stable_project_roots(slug):
                yield
            return
        project_root = self.backend_root / "app" / "projects" / slug
        with stable_directories(self.backend_root, self.backend_root / "app", self.backend_root / "app" / "projects", project_root):
            yield

    def invoke(self, slug: str, path: str, method: str, query: str, body: bytes, content_type: str | None) -> RuntimeResponse:
        slug = validate_slug(slug)
        path = validate_runtime_path(path)
        request = json.dumps({"method": method, "path": path, "query": query, "body": body.decode("utf-8", "strict"), "content_type": content_type or ""}, separators=(",", ":")).encode("utf-8")
        runner_argv = [sys.executable, "-I", "-B", str(self.runner)]
        if self.sealed_source is not None:
            runner_argv.extend(["--sealed-archive", str(self.backend_root), slug])
            cwd = self.runner.parent
            pass_fds = self.sealed_source.inherited_fds
        else:
            runner_argv.extend([str(self.backend_root), slug])
            cwd = self.backend_root
            pass_fds = ()
        try:
            with self._source_guard(slug), _runtime_slot(slug):
                completed = _run(
                    runner_argv,
                    cwd=cwd,
                    input_data=request,
                    timeout=self.timeout_seconds,
                    environment=sanitized_environment({"PYTHONIOENCODING": "utf-8"}),
                    max_output=10 * 1024 * 1024 + 65536,
                    memory_bytes=2 * 1024 * 1024 * 1024,
                    max_processes=32,
                    pass_fds=pass_fds,
                )
        except BuildFailed as exc:
            code = "runtime_timeout" if exc.code == "build_timeout" else "runtime_limits_unavailable" if exc.code == "build_limits_unavailable" else "runtime_unavailable"
            status = 504 if code == "runtime_timeout" else 409
            raise PublicError(code, "project runtime is unavailable", status, recoverable=True) from exc
        except OSError as exc:
            raise PublicError("runtime_unavailable", "project runtime is unavailable", 409, recoverable=True) from exc
        if completed.returncode != 0 or completed.stdout_truncated or completed.stderr_truncated:
            raise PublicError("runtime_failed", "project runtime failed", 502)
        try:
            result = json.loads(completed.stdout)
            return RuntimeResponse(int(result["status"]), str(result["content_type"]), str(result["body"]).encode("utf-8"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PublicError("runtime_protocol_error", "project runtime returned an invalid response", 502) from exc


class IsolatedGatewayRuntimeProvider:
    isolated = True

    def __init__(self, base_url: str, token_env: str, allowed_hosts: tuple[str, ...], timeout_seconds: int = 30) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(base_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("isolated runtime gateway must use a safe HTTPS URL")
        if parsed.hostname.casefold() not in {host.casefold() for host in allowed_hosts}:
            raise ValueError("isolated runtime gateway host is not allowlisted")
        if not token_env or not os.environ.get(token_env):
            raise ValueError("isolated runtime gateway token is unavailable")
        self.base_url = base_url.rstrip("/")
        self.token_env = token_env
        self.timeout_seconds = min(max(int(timeout_seconds), 1), 60)

    def invoke(self, slug: str, path: str, method: str, query: str, body: bytes, content_type: str | None) -> RuntimeResponse:
        from urllib.error import HTTPError
        from urllib.parse import quote
        from urllib.request import HTTPRedirectHandler, Request, build_opener

        slug = validate_slug(slug)
        path = validate_runtime_path(path)

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        target = f"{self.base_url}/v1/projects/{quote(slug, safe='')}/app/{quote(path, safe='/')}"
        if query:
            target += "?" + query
        headers = {
            "Authorization": "Bearer " + os.environ[self.token_env],
            "Content-Type": content_type or "application/octet-stream",
            "X-Toolbox-Runtime-Contract": "isolated-gateway-v1",
        }
        request = Request(target, data=body if method in {"POST", "PUT", "PATCH"} else None, headers=headers, method=method)
        try:
            response = build_opener(NoRedirect()).open(request, timeout=self.timeout_seconds)
            payload = response.read(10 * 1024 * 1024 + 1)
            status = int(response.status)
            response_type = response.headers.get_content_type()
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise PublicError("runtime_redirect_blocked", "runtime redirects are not allowed", 502) from exc
            payload = exc.read(10 * 1024 * 1024 + 1)
            status = int(exc.code)
            response_type = exc.headers.get_content_type()
        except OSError as exc:
            raise PublicError("runtime_unavailable", "isolated runtime gateway is unavailable", 503, recoverable=True) from exc
        if len(payload) > 10 * 1024 * 1024:
            raise PublicError("runtime_response_too_large", "runtime response exceeds 10 MiB", 502)
        return RuntimeResponse(status, response_type, payload)


class UnavailableIsolatedRuntimeProvider:
    isolated = True

    def invoke(self, slug: str, path: str, method: str, query: str, body: bytes, content_type: str | None) -> RuntimeResponse:
        del slug, path, method, query, body, content_type
        raise PublicError("runtime_unavailable", "configured isolated runtime gateway is unavailable", 409)
