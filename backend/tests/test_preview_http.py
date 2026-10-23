from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.errors import PublicError
from app.core.studio_config import StudioConfig
from app.preview_app import PreviewBodyLimitMiddleware, create_preview_app
from app.projects.runtime import IsolatedGatewayRuntimeProvider, LocalProjectRuntimeProvider, RuntimeResponse, SealedRuntimeSource
from app.submissions.database import connect
from app.submissions.migrations import migrate
from app.submissions.preview import PreviewManager


class FakeRuntime:
    def __init__(self, root, calls):
        self.root = root
        self.calls = calls

    def invoke(self, slug, path, method, query, body, content_type):
        self.calls.append({"root": self.root, "slug": slug, "path": path, "method": method, "query": query, "body": body, "content_type": content_type})
        if path == "redirect":
            return RuntimeResponse(302, "text/plain", b"redirect")
        if path == "unavailable":
            raise PublicError("runtime_unavailable", "runtime provider unavailable", 503, recoverable=True)
        return RuntimeResponse(200, "application/json", b'{"ok":true}')


def _sealed_preview(tmp_path):
    database = tmp_path / "preview.sqlite3"
    migrate(database)
    root = tmp_path / "previews"
    manager = PreviewManager(database, root)
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text("safe", encoding="utf-8")
    runtime = tmp_path / "snapshot" / "backend"
    route = runtime / "app" / "projects" / "demo" / "routes.py"
    route.parent.mkdir(parents=True)
    route.write_text("sealed = True\n", encoding="utf-8")
    preview = manager.seal(subject_kind="workspace", subject_id="demo", source=source, input_sha256="i", harness_sha256="h", runtime_root=runtime, project_slug="demo")
    grant = manager.create_grant(preview["id"], subject_kind="workspace", subject_id="demo", actor="alice")
    return database, root, manager, preview, grant


def test_submission_runtime_uses_only_the_sealed_backend_root(tmp_path, monkeypatch):
    runtime = tmp_path / "sealed" / "backend"
    route = runtime / "app" / "projects" / "demo" / "routes.py"
    route.parent.mkdir(parents=True)
    route.write_text(
        "from flask import Blueprint, jsonify\n"
        "blueprint = Blueprint('sealed_demo', __name__)\n"
        "@blueprint.get('/ping')\n"
        "def ping(): return jsonify({'source': 'sealed'})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.projects.registry.stable_project_roots",
        lambda _slug: (_ for _ in ()).throw(AssertionError("live workspace must not be consulted")),
    )
    provider = LocalProjectRuntimeProvider(runtime, workspace_guard=False)
    response = provider.invoke("demo", "ping", "GET", "", b"", None)
    assert response.status_code == 200
    assert json.loads(response.body) == {"source": "sealed"}


def test_local_runtime_can_import_a_descriptor_bound_archive(tmp_path):
    archive_path = tmp_path / "sealed-runtime.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("app/__init__.py", "")
        archive.writestr("app/projects/__init__.py", "")
        archive.writestr("app/projects/demo/__init__.py", "")
        archive.writestr(
            "app/projects/demo/routes.py",
            "from flask import Blueprint, jsonify\n"
            "blueprint = Blueprint('sealed_archive_demo', __name__)\n"
            "@blueprint.get('/ping')\n"
            "def ping(): return jsonify({'source': 'sealed-archive'})\n",
        )
    provider = LocalProjectRuntimeProvider(SealedRuntimeSource(archive_path, ()), workspace_guard=False)
    response = provider.invoke("demo", "ping", "GET", "", b"", None)
    assert response.status_code == 200
    assert json.loads(response.body) == {"source": "sealed-archive"}


def test_preview_cookie_asset_handle_headers_and_runtime_isolation(tmp_path, monkeypatch):
    database, root, manager, preview, grant = _sealed_preview(tmp_path)
    calls = []
    config = StudioConfig(database_path=database)
    with TestClient(create_preview_app(config=config, database_path=database, preview_root=root, runtime_factory=lambda runtime_root: FakeRuntime(runtime_root, calls))) as client:
        exchanged = client.post("/exchange", json={"nonce": grant["nonce"], "preview_id": preview["id"], "subject_kind": "workspace", "subject_id": "demo"})
        assert exchanged.status_code == 200
        cookie = exchanged.headers["set-cookie"]
        assert "tb_preview_local=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie and "Domain=" not in cookie
        response = client.get(exchanged.json()["url"])
        assert response.status_code == 200 and response.text == "safe"
        csp = response.headers["content-security-policy"]
        assert "connect-src 'self'" in csp and "sandbox allow-scripts allow-same-origin" in csp
        assert client.get(f"/preview/{preview['id']}/../outside").status_code != 200

        allowed = client.post("/api/projects/demo/app/ping?value=1", json={"safe": True}, headers={"authorization": "must-not-forward", "x-correlation-id": "preview-test"})
        assert allowed.status_code == 200 and allowed.json() == {"ok": True}
        assert calls == [{"root": root.parent / "snapshot" / "backend", "slug": "demo", "path": "ping", "method": "POST", "query": "value=1", "body": b'{"safe":true}', "content_type": "application/json"}]
        for unsafe_path in ("%2e%2e/admin", "safe%2fadmin", "safe%5cadmin", "safe//admin"):
            assert client.get(f"/api/projects/demo/app/{unsafe_path}").status_code in {404, 422}
        assert len(calls) == 1
        assert allowed.headers["x-correlation-id"] == "preview-test"
        assert client.get("/api/projects/other/app/ping").status_code == 403
        assert client.delete("/api/projects/demo/app/ping").status_code == 405
        assert client.post("/api/projects/demo/app/ping", content=b"x", headers={"content-type": "application/xml"}).status_code == 415
        assert client.get("/api/projects/demo/app/redirect").status_code == 502
        unavailable = client.get("/api/projects/demo/app/unavailable")
        assert unavailable.status_code == 503 and unavailable.json()["error"] == "runtime_unavailable"

        async def force_timeout(awaitable, timeout):
            del timeout
            awaitable.close()
            raise asyncio.TimeoutError

        with monkeypatch.context() as timeout_patch:
            timeout_patch.setattr("app.preview_app.asyncio.wait_for", force_timeout)
            timed_out = client.get("/api/projects/demo/app/timeout")
        assert timed_out.status_code == 504 and timed_out.json()["error"] == "runtime_timeout", timed_out.text
        for blocked in ("/api/session", "/api/submissions", "/api/config/owners", "/api/connections/x/status", "/api/browser/x", "/api/projects/demo/files", "/api/projects/demo/manifest"):
            assert client.get(blocked).status_code == 404

        asset = root / preview["root_ref"][:2] / preview["root_ref"] / "index.html"
        assert asset.stat().st_mode & stat.S_IWUSR == 0
        os.chmod(asset, stat.S_IREAD | stat.S_IWRITE)
        asset.write_text("tampered", encoding="utf-8")
        assert client.get(exchanged.json()["url"]).status_code == 409

    connection = connect(database)
    events = connection.execute(
        "SELECT actor_id,digest,method,runtime_path,status_code,correlation_id,error_code FROM preview_runtime_events ORDER BY id"
    ).fetchall()
    preview_state = connection.execute("SELECT state,error_code FROM preview_objects WHERE id=?", (preview["id"],)).fetchone()
    assert tuple(preview_state) == ("unavailable", "preview_integrity_failed")
    connection.close()
    assert {row["runtime_path"]: (row["status_code"], row["error_code"]) for row in events} == {
        "ping": (200, None),
        "redirect": (502, "runtime_redirect_blocked"),
        "unavailable": (503, "runtime_unavailable"),
        "timeout": (504, "runtime_timeout"),
    }
    event = dict(events[0])
    assert event == {"actor_id": "alice", "digest": preview["digest"], "method": "POST", "runtime_path": "ping", "status_code": 200, "correlation_id": "preview-test", "error_code": None}


def test_workspace_preview_supersession_denies_old_asset_runtime_and_pending_exchange(tmp_path):
    database, root, manager, first, first_grant = _sealed_preview(tmp_path)
    pending_grant = manager.create_grant(first["id"], subject_kind="workspace", subject_id="demo", actor="alice")
    config = StudioConfig(database_path=database)
    with TestClient(create_preview_app(config=config, database_path=database, preview_root=root, runtime_factory=lambda runtime_root: FakeRuntime(runtime_root, []))) as client:
        exchanged = client.post(
            "/exchange",
            json={"nonce": first_grant["nonce"], "preview_id": first["id"], "subject_kind": "workspace", "subject_id": "demo"},
        )
        assert exchanged.status_code == 200
        assert client.get(exchanged.json()["url"]).status_code == 200

        second_source = tmp_path / "build-second"
        second_source.mkdir()
        (second_source / "index.html").write_text("current", encoding="utf-8")
        second_runtime = tmp_path / "snapshot-second" / "backend"
        second_route = second_runtime / "app" / "projects" / "demo" / "routes.py"
        second_route.parent.mkdir(parents=True)
        second_route.write_text("sealed = 'second'\n", encoding="utf-8")
        second = manager.seal(
            subject_kind="workspace", subject_id="demo", source=second_source,
            input_sha256="second", harness_sha256="h", runtime_root=second_runtime, project_slug="demo",
        )

        old_asset = client.get(exchanged.json()["url"])
        old_runtime = client.get("/api/projects/demo/app/ping")
        pending_exchange = client.post(
            "/exchange",
            json={"nonce": pending_grant["nonce"], "preview_id": first["id"], "subject_kind": "workspace", "subject_id": "demo"},
        )
        assert old_asset.status_code == 403 and old_asset.json()["error"] == "preview_unauthorized"
        assert old_runtime.status_code == 403 and old_runtime.json()["error"] == "preview_runtime_unauthorized"
        assert pending_exchange.status_code == 403 and pending_exchange.json()["error"] == "invalid_preview_grant"

        current_grant = manager.create_grant(second["id"], subject_kind="workspace", subject_id="demo", actor="alice")
        current_exchange = client.post(
            "/exchange",
            json={"nonce": current_grant["nonce"], "preview_id": second["id"], "subject_kind": "workspace", "subject_id": "demo"},
        )
        assert current_exchange.status_code == 200
        assert client.get(current_exchange.json()["url"]).text == "current"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-bound runtime regression")
def test_preview_runtime_executes_opened_bytes_when_original_root_is_replaced(tmp_path):
    database = tmp_path / "descriptor-runtime.sqlite3"
    migrate(database)
    root = tmp_path / "previews"
    manager = PreviewManager(database, root)
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text("safe", encoding="utf-8")
    runtime = tmp_path / "snapshot" / "backend"
    route = runtime / "app" / "projects" / "demo" / "routes.py"
    route.parent.mkdir(parents=True)
    route.write_text(
        "from flask import Blueprint, jsonify\n"
        "blueprint = Blueprint('sealed_demo', __name__)\n"
        "@blueprint.get('/ping')\n"
        "def ping(): return jsonify({'source': 'sealed'})\n",
        encoding="utf-8",
    )
    preview = manager.seal(
        subject_kind="workspace", subject_id="demo", source=source, input_sha256="i", harness_sha256="h",
        runtime_root=runtime, project_slug="demo",
    )

    with manager.open_runtime(preview) as sealed_source:
        retained = runtime.with_name("backend-retained")
        runtime.rename(retained)
        malicious = runtime / "app" / "projects" / "demo" / "routes.py"
        malicious.parent.mkdir(parents=True)
        malicious.write_text(
            "from flask import Blueprint, jsonify\n"
            "blueprint = Blueprint('replacement_demo', __name__)\n"
            "@blueprint.get('/ping')\n"
            "def ping(): return jsonify({'source': 'replacement'})\n",
            encoding="utf-8",
        )
        response = LocalProjectRuntimeProvider(sealed_source, workspace_guard=False).invoke(
            "demo", "ping", "GET", "", b"", None,
        )

    assert response.status_code == 200
    assert json.loads(response.body) == {"source": "sealed"}


def test_preview_runtime_integrity_error_matches_audit_event(tmp_path):
    database, root, _manager, preview, grant = _sealed_preview(tmp_path)
    config = StudioConfig(database_path=database)
    with TestClient(create_preview_app(config=config, database_path=database, preview_root=root, runtime_factory=lambda runtime_root: FakeRuntime(runtime_root, []))) as client:
        exchanged = client.post(
            "/exchange",
            json={"nonce": grant["nonce"], "preview_id": preview["id"], "subject_kind": "workspace", "subject_id": "demo"},
        )
        assert exchanged.status_code == 200
        runtime_file = root.parent / "snapshot" / "backend" / "app" / "projects" / "demo" / "routes.py"
        os.chmod(runtime_file, stat.S_IREAD | stat.S_IWRITE)
        runtime_file.write_text("tampered = True\n", encoding="utf-8")
        response = client.get("/api/projects/demo/app/ping")
        assert response.status_code == 409
        assert response.json()["error"] == "preview_integrity_failed"
    connection = connect(database)
    event = connection.execute(
        "SELECT status_code,error_code FROM preview_runtime_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    connection.close()
    assert dict(event) == {"status_code": 409, "error_code": "preview_integrity_failed"}


def test_preview_assets_and_runtime_exclude_in_place_writers_while_consumed(tmp_path):
    database, root, manager, preview, _grant = _sealed_preview(tmp_path)
    asset = root / preview["root_ref"][:2] / preview["root_ref"] / "index.html"
    runtime = root.parent / "snapshot" / "backend" / "app" / "projects" / "demo" / "routes.py"
    assert asset.stat().st_mode & stat.S_IWUSR == 0
    assert runtime.stat().st_mode & stat.S_IWUSR == 0

    os.chmod(asset, stat.S_IREAD | stat.S_IWRITE)
    if os.name == "nt":
        with manager.open_asset(preview, "index.html"):
            with pytest.raises(OSError):
                asset.write_text("changed-after-verification", encoding="utf-8")
    else:
        with pytest.raises(PublicError, match="sealed content changed"):
            with manager.open_asset(preview, "index.html"):
                asset.write_text("changed-after-verification", encoding="utf-8")

    connection = connect(database)
    state = connection.execute("SELECT state FROM preview_objects WHERE id=?", (preview["id"],)).fetchone()[0]
    connection.close()
    if os.name == "nt":
        assert state == "ready"
        os.chmod(runtime, stat.S_IREAD | stat.S_IWRITE)
        with manager.open_runtime(preview):
            with pytest.raises(OSError):
                runtime.write_text("changed_after_verification = True\n", encoding="utf-8")
    else:
        assert state == "unavailable"


def test_preview_rejects_declared_and_chunked_bodies_before_parsing(tmp_path):
    database = tmp_path / "limits.sqlite3"
    migrate(database)
    with TestClient(create_preview_app(config=StudioConfig(database_path=database), database_path=database, preview_root=tmp_path / "previews")) as client:
        response = client.post("/exchange", content=b"x" * (64 * 1024 + 1), headers={"content-type": "application/json"})
        assert response.status_code == 413

    called = False
    chunks = [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56", "more_body": False},
    ]
    sent = []

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(PreviewBodyLimitMiddleware(downstream, exchange_limit=5)(
        {"type": "http", "method": "POST", "path": "/exchange", "headers": [], "state": {"correlation_id": "preview-limit"}},
        receive,
        send,
    ))
    assert called is False
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["correlation_id"] == "preview-limit"


def test_gateway_provider_rejects_noncanonical_paths_before_network(monkeypatch):
    monkeypatch.setenv("RUNTIME_GATEWAY_TOKEN", "secret")
    provider = IsolatedGatewayRuntimeProvider(
        "https://runtime.example", "RUNTIME_GATEWAY_TOKEN", ("runtime.example",),
    )
    for unsafe_path in ("../../admin", "safe//admin", "safe/./admin", "safe\\admin", "%2e%2e/admin", "safe%2fadmin", "safe\x00admin"):
        with pytest.raises(PublicError) as rejected:
            provider.invoke("demo", unsafe_path, "GET", "", b"", None)
        assert getattr(rejected.value, "code", None) == "invalid_runtime_path"


def test_https_cookie_uses_host_prefix_from_standard_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_GATEWAY_TOKEN", "secret")
    database, root, _manager, preview, grant = _sealed_preview(tmp_path)
    config = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=database,
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",),
        preview_origin="https://preview.example", preview_port=443,
        preview_allowed_origins=("https://preview.example",), artifact_provider="s3", build_provider="codebuild",
        codebuild_image_digest="a" * 64, codebuild_wrapper_sha256="b" * 64,
        codebuild_probe_version="probe-v1", codebuild_account_id="111122223333", codebuild_region="eu-west-1",
        runtime_provider="isolated-gateway", proxy_hmac_secret="secret",
        runtime_gateway_url="https://runtime.example", runtime_gateway_token_env="RUNTIME_GATEWAY_TOKEN",
        runtime_gateway_allowed_hosts=("runtime.example",),
    )
    with TestClient(create_preview_app(config=config, database_path=database, preview_root=root), base_url="https://preview.example") as client:
        response = client.post("/exchange", json={"nonce": grant["nonce"], "preview_id": preview["id"], "subject_kind": "workspace", "subject_id": "demo"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "__Host-tb_preview=" in cookie
        assert "Secure" in cookie and "Path=/" in cookie and "Domain=" not in cookie


def test_preview_cookie_mode_rejects_incoherent_override(tmp_path):
    database = tmp_path / "secure.sqlite3"
    migrate(database)
    with pytest.raises(ValueError, match="cookie security"):
        create_preview_app(config=StudioConfig(database_path=database), database_path=database, preview_root=tmp_path / "secure", secure_cookie=True)


def test_shared_preview_uses_configured_isolated_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_GATEWAY_TOKEN", "secret")
    calls = []

    def invoke(_provider, slug, path, method, query, body, content_type):
        calls.append((slug, path, method, query, body, content_type))
        return RuntimeResponse(200, "application/json", b'{"shared":true}')

    monkeypatch.setattr("app.preview_app.IsolatedGatewayRuntimeProvider.invoke", invoke)
    database, root, _manager, preview, grant = _sealed_preview(tmp_path)
    config = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=database,
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",),
        preview_origin="https://preview.example", preview_port=443,
        preview_allowed_origins=("https://preview.example",), artifact_provider="s3", build_provider="codebuild",
        codebuild_image_digest="a" * 64, codebuild_wrapper_sha256="b" * 64,
        codebuild_probe_version="probe-v1", codebuild_account_id="111122223333", codebuild_region="eu-west-1",
        runtime_provider="isolated-gateway", proxy_hmac_secret="secret",
        runtime_gateway_url="https://runtime.example", runtime_gateway_token_env="RUNTIME_GATEWAY_TOKEN",
        runtime_gateway_allowed_hosts=("runtime.example",),
    )
    with TestClient(create_preview_app(config=config, database_path=database, preview_root=root), base_url="https://preview.example") as client:
        exchanged = client.post("/exchange", json={"nonce": grant["nonce"], "preview_id": preview["id"], "subject_kind": "workspace", "subject_id": "demo"})
        assert exchanged.status_code == 200
        runtime = client.get("/api/projects/demo/app/ping")
        assert runtime.status_code == 200
        assert runtime.json() == {"shared": True}
        assert calls == [("demo", "ping", "GET", "", b"", None)]
