from __future__ import annotations

import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.asgi import create_app  # noqa: E402
from app.core.studio_config import StudioConfig  # noqa: E402
from app.projects.runtime import LocalProjectRuntimeProvider, RuntimeResponse  # noqa: E402
from app.submissions.database import connect, transaction  # noqa: E402
from app.submissions.migrations import migrate  # noqa: E402
from app.submissions.repository import SubmissionRepository  # noqa: E402
from app.submissions.repository_v2 import PipelineRepository  # noqa: E402


def _wait_ready(client, timeout=2):
    deadline = time.monotonic() + timeout
    response = client.get("/api/ready")
    while response.status_code != 200 and time.monotonic() < deadline:
        time.sleep(0.01)
        response = client.get("/api/ready")
    return response


def test_fastapi_health_ready_docs_and_owner_contract(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_OWNER_EMAILS", raising=False)
    app = create_app(StudioConfig(database_path=tmp_path / "studio.sqlite3"))
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert _wait_ready(client).json() == {"status": "ready"}
        assert client.get("/docs").status_code == 200
        client.get("/api/session")
        owners = client.get("/api/config/owners").json()
        assert set(owners) == {"owners", "email_enabled", "slack_enabled"}
        assert "x-correlation-id" in client.get("/api/health").headers


def test_startup_exposes_health_while_interrupted_submission_recovers(tmp_path):
    database = tmp_path / "recovery.sqlite3"
    migrate(database)
    submission_id = "sub_" + "9" * 32
    operation_id = "op_" + "9" * 32
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(
            submission_id=submission_id, slug="demo-ping", version=1, actor="alice",
            title="T", description="D", correlation_id="original-correlation",
        )
        connection.execute(
            "INSERT INTO idempotency_records(scope,actor_id,route_template,resource_id,key,request_sha256,status,operation_id,subject_id,expires_at) "
            "VALUES ('submission','alice','/api/submissions','demo-ping','startup-recovery','hash','pending',?,?,datetime('now','+1 day'))",
            (operation_id, submission_id),
        )
    connection.close()
    app = create_app(StudioConfig(database_path=database))
    started = threading.Event()
    release = threading.Event()

    def slow_recovery(_operation_id, _submission_id):
        started.set()
        release.wait(5)

    app.state.submission_saga.run = slow_recovery
    try:
        with TestClient(app) as client:
            assert started.wait(2)
            assert client.get("/api/health").status_code == 200
            ready = client.get("/api/ready")
            assert ready.status_code == 503
            assert ready.json()["status"] == "starting"
            assert ready.json()["submission_recovery"] == {"status": "pending", "error_code": "submission_recovery_pending"}
            release.set()
    finally:
        release.set()


def test_flask_bridge_is_authorized_by_asgi_layer(tmp_path):
    app = create_app(StudioConfig(database_path=tmp_path / "studio.sqlite3"))
    with TestClient(app) as client:
        client.get("/api/session")
        response = client.get("/api/connections/unknown/status")
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_connection"
        multipart = client.post(
            "/api/manifest/upload",
            files={"dummy": ("dummy.txt", b"x", "text/plain")},
            data={"station_code": "TEST"},
            headers={"x-csrf-token": client.cookies.get("studio_csrf")},
        )
        assert multipart.status_code == 400
        assert multipart.json()["error"] == "missing_file"
        assert client.delete("/api/projects/anything").status_code == 404


def test_project_runtime_compatibility_runs_out_of_process(tmp_path):
    app = create_app(StudioConfig(database_path=tmp_path / "studio.sqlite3"))
    with TestClient(app) as client:
        client.get("/api/session")
        response = client.get("/api/projects/demo-ping/app/ping")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "project": "demo-ping"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_project_runtime_does_not_block_real_uvicorn_health_or_lists(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_runtime(_self, _slug, _path, _method, _query, _body, _content_type):
        started.set()
        assert release.wait(5)
        return RuntimeResponse(200, "application/json", b"{}")

    monkeypatch.setattr(LocalProjectRuntimeProvider, "invoke", blocked_runtime)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(StudioConfig(database_path=tmp_path / "runtime-concurrency.sqlite3"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, proxy_headers=False, log_level="warning", access_log=False))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        session = httpx.get(f"http://127.0.0.1:{port}/api/session", timeout=5)
        assert session.status_code == 200
        cookies = dict(session.cookies)
        with ThreadPoolExecutor(max_workers=1) as pool:
            runtime = pool.submit(
                httpx.get,
                f"http://127.0.0.1:{port}/api/projects/demo-ping/app/ping",
                cookies=cookies,
                timeout=5,
            )
            assert started.wait(2)
            began = time.monotonic()
            health = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)
            projects = httpx.get(f"http://127.0.0.1:{port}/api/projects", cookies=cookies, timeout=2)
            elapsed = time.monotonic() - began
            assert health.status_code == 200
            assert projects.status_code == 200
            assert elapsed < 2.0
            release.set()
            assert runtime.result(timeout=5).status_code == 200
    finally:
        release.set()
        server.should_exit = True
        server_thread.join(timeout=10)
    assert not server_thread.is_alive()


def test_project_runtime_forces_active_content_to_safe_download(tmp_path, monkeypatch):
    def active_response(_self, _slug, path, _method, _query, _body, _content_type):
        content_type = "text/html" if path == "html" else "image/svg+xml"
        return RuntimeResponse(200, content_type, b"<script>document.cookie</script>")

    monkeypatch.setattr(LocalProjectRuntimeProvider, "invoke", active_response)
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "active.sqlite3"))) as client:
        client.get("/api/session")
        assert client.cookies.get("studio_csrf")
        for path in ("html", "svg"):
            response = client.get(f"/api/projects/demo-ping/app/{path}")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/octet-stream")
            assert response.headers["content-disposition"].startswith("attachment;")
            assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "no-store"


def test_project_runtime_rejects_encoded_or_noncanonical_paths(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(LocalProjectRuntimeProvider, "invoke", lambda *args: calls.append(args) or RuntimeResponse(200, "application/json", b"{}"))
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "paths.sqlite3"))) as client:
        client.get("/api/session")
        for path in ("%2e%2e/admin", "safe%2fadmin", "safe%5cadmin", "safe//admin"):
            assert client.get(f"/api/projects/demo-ping/app/{path}").status_code in {404, 422}
        assert calls == []


def test_outbox_provider_cannot_block_health_or_authenticated_lists(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_dispatch(_self, limit=100):
        del limit
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr("app.asgi.OutboxDispatcher.dispatch_pending", blocked_dispatch)
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "outbox.sqlite3"))) as client:
        try:
            assert started.wait(2)
            assert client.get("/api/health").status_code == 200
            client.get("/api/session")
            assert client.get("/api/projects").status_code == 200
        finally:
            release.set()


def test_recovery_failure_degrades_ready_until_a_confirmed_pass(tmp_path, monkeypatch):
    failed = threading.Event()
    retry_started = threading.Event()
    allow_success = threading.Event()
    calls = 0

    def recover(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            failed.set()
            raise RuntimeError("sensitive internal failure")
        retry_started.set()
        allow_success.wait(5)
        return []

    monkeypatch.setattr("app.asgi.recover_interrupted_submissions", recover)
    monkeypatch.setattr("app.asgi._RECOVERY_INTERVAL_SECONDS", 0.05)
    database = tmp_path / "recovery-health.sqlite3"
    with TestClient(create_app(StudioConfig(database_path=database))) as client:
        try:
            assert failed.wait(2)
            deadline = time.monotonic() + 2
            degraded = None
            while time.monotonic() < deadline:
                degraded = client.get("/api/ready")
                if degraded.status_code == 503 and degraded.json().get("submission_recovery", {}).get("status") == "failed":
                    break
                time.sleep(0.01)
            assert degraded is not None and degraded.status_code == 503
            assert degraded.json()["submission_recovery"] == {"status": "failed", "error_code": "submission_recovery_failed"}
            assert "sensitive" not in degraded.text
            assert client.get("/api/health").status_code == 200
            assert retry_started.wait(2)
            allow_success.set()
            deadline = time.monotonic() + 2
            recovered = None
            while time.monotonic() < deadline:
                recovered = client.get("/api/ready")
                if recovered.status_code == 200:
                    break
                time.sleep(0.01)
            assert recovered is not None and recovered.status_code == 200
            connection = connect(database)
            status = connection.execute("SELECT status,error_code FROM operational_status WHERE key='submission_recovery'").fetchone()
            connection.close()
            assert dict(status) == {"status": "ok", "error_code": None}
        finally:
            allow_success.set()
