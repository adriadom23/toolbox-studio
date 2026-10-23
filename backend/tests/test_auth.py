from __future__ import annotations

import hashlib
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.asgi import create_app  # noqa: E402
from app.core.security import proxy_signature  # noqa: E402
from app.core.studio_config import StudioConfig  # noqa: E402
from app.submissions.database import connect, transaction  # noqa: E402


def test_local_session_is_http_only_and_mutations_require_csrf(tmp_path):
    app = create_app(StudioConfig(database_path=tmp_path / "local.sqlite3"))
    with TestClient(app) as client:
        assert client.post("/api/projects", json={"name": "First Post"}).status_code == 401
        response = client.get("/api/session")
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.post("/api/projects", json={"name": "Auth Test"}).status_code == 403
        csrf = client.cookies.get("studio_csrf")
        assert client.post("/api/projects", json={"name": "123"}, headers={"x-csrf-token": csrf, "Idempotency-Key": "auth-invalid-name"}).status_code == 400


def test_local_roles_are_taken_from_configuration_without_elevation(tmp_path):
    config = StudioConfig(database_path=tmp_path / "submitter.sqlite3", identity_roles={"local-user": ("submitter",)})
    with TestClient(create_app(config)) as client:
        session = client.get("/api/session")
        assert session.json()["roles"] == ["submitter"]
        projects = client.get("/api/projects")
        assert projects.status_code == 200
        assert {item["name"] for item in projects.json()} >= {"demo-ping", "k"}
        assert client.get("/api/projects/demo-ping").status_code == 200
        csrf = {"x-csrf-token": client.cookies.get("studio_csrf")}
        assert client.post("/api/projects/demo-ping/claim", json={"expected_version": 1}, headers=csrf).status_code == 403
        assert client.put("/api/projects/demo-ping/members/bob", json={"active": True, "expected_version": 0}, headers=csrf).status_code == 403


def test_local_project_create_capability_matches_endpoint_roles(tmp_path):
    cases = (
        (("reviewer",), False),
        (("submitter",), True),
        (("admin",), True),
    )
    for index, (roles, allowed) in enumerate(cases):
        config = StudioConfig(
            database_path=tmp_path / f"project-create-{index}.sqlite3",
            identity_roles={"local-user": roles},
        )
        with TestClient(create_app(config)) as client:
            session = client.get("/api/session")
            assert ("project.create" in session.json()["capabilities"]) is allowed
            response = client.post(
                "/api/projects",
                json={"name": "123"},
                headers={
                    "x-csrf-token": client.cookies.get("studio_csrf"),
                    "Idempotency-Key": f"role-project-{index}",
                },
            )
            assert response.status_code == (400 if allowed else 403)


def test_real_uvicorn_ignores_spoofed_forwarded_peer(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(StudioConfig(database_path=tmp_path / "uvicorn.sqlite3"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, proxy_headers=False, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/api/session", headers={"x-forwarded-for": "203.0.113.50"})
        assert response.status_code == 200
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()


def _isolated_providers():
    isolated = SimpleNamespace(isolated=True)
    return SimpleNamespace(
        artifacts=SimpleNamespace(provider="s3"), build=isolated, runtime=isolated,
        logs=SimpleNamespace(), notifications=SimpleNamespace(), publication=SimpleNamespace(), git=SimpleNamespace(),
    )


def _signed_headers(secret: str, *, nonce: str, path: str = "/api/projects", roles: str = "submitter", identity: str = "alice"):
    timestamp = str(time.time())
    request_id = "req-" + nonce
    signature = proxy_signature(
        secret, identity, timestamp, nonce, "GET", path,
        query="", body_sha256=hashlib.sha256(b"").hexdigest(), roles=roles, request_id=request_id,
    )
    return {
        "x-studio-identity": identity, "x-studio-roles": roles,
        "x-studio-timestamp": timestamp, "x-studio-nonce": nonce,
        "x-studio-request-id": request_id, "x-studio-signature": signature,
    }


def test_shared_proxy_rejects_invalid_and_ambiguous_identities_before_nonce_reservation(tmp_path):
    config = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "identities.sqlite3",
        proxy_hmac_secret="secret", trusted_peer_cidrs=("127.0.0.1/32",),
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",), preview_origin="https://preview.example",
        preview_port=443, preview_allowed_origins=("https://preview.example",), artifact_provider="s3",
        build_provider="codebuild", runtime_provider="isolated-gateway",
        codebuild_image_digest="a" * 64, codebuild_wrapper_sha256="b" * 64,
        codebuild_probe_version="probe-v1", codebuild_account_id="111122223333", codebuild_region="eu-west-1",
        identity_mapping={"mapped": "bad\nactor"},
    )
    with TestClient(create_app(config, providers=_isolated_providers()), client=("127.0.0.1", 50000)) as client:
        invalid = ("", "alice\nadmin", "a" * 201, "mapped")
        for index, identity in enumerate(invalid):
            response = client.get(
                "/api/projects",
                headers=_signed_headers("secret", nonce=f"invalid-{index}", identity=identity),
            )
            assert response.status_code == 401
            assert response.json()["error"] == "invalid_proxy_identity"
    connection = connect(config.database_path)
    assert connection.execute("SELECT COUNT(*) FROM proxy_replay_nonces").fetchone()[0] == 0
    connection.close()


def test_shared_profile_requires_isolated_contracts_and_signature_each_request(tmp_path):
    unsafe = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "unsafe.sqlite3",
        proxy_hmac_secret="secret", public_base_url="https://studio.example",
        review_base_url="https://studio.example", cors_origins=("https://studio.example",),
        preview_origin="https://preview.example", preview_port=443,
        preview_allowed_origins=("https://preview.example",),
    )
    with pytest.raises(ValueError, match="isolated providers"):
        create_app(unsafe)

    config = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "shared.sqlite3",
        proxy_hmac_secret="secret", trusted_peer_cidrs=("127.0.0.1/32",),
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",), preview_origin="https://preview.example",
        preview_port=443, preview_allowed_origins=("https://preview.example",), artifact_provider="s3",
        build_provider="codebuild", runtime_provider="isolated-gateway",
        codebuild_image_digest="a" * 64, codebuild_wrapper_sha256="b" * 64,
        codebuild_probe_version="probe-v1", codebuild_account_id="111122223333", codebuild_region="eu-west-1",
    )
    app = create_app(config, providers=_isolated_providers())
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/projects", headers={"x-studio-identity": "alice"}).status_code == 401
        assert client.get("/api/projects", headers=_signed_headers("secret", nonce="nonce-1")).status_code == 200
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/projects", headers=_signed_headers("secret", nonce="nonce-1")).status_code == 401

        connection = connect(config.database_path)
        assert connection.execute("SELECT owner_id FROM projects WHERE slug='demo-ping'").fetchone()[0] is None
        with transaction(connection):
            connection.execute("UPDATE projects SET owner_id='alice' WHERE slug='demo-ping'")
        connection.close()
        alice = client.get("/api/projects/demo-ping", headers=_signed_headers("secret", nonce="nonce-2", path="/api/projects/demo-ping"))
        assert alice.status_code == 200
        bob = client.get("/api/projects/demo-ping", headers=_signed_headers("secret", nonce="nonce-3", path="/api/projects/demo-ping", identity="bob"))
        assert bob.status_code == 404
        bridge = client.get("/api/connections/unknown/status", headers=_signed_headers("secret", nonce="nonce-4", path="/api/connections/unknown/status", roles="admin"))
        assert bridge.status_code == 404


def test_shared_ready_is_aggregate_and_admin_details_are_authenticated_and_audited(tmp_path):
    config = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "ready-shared.sqlite3",
        proxy_hmac_secret="secret", trusted_peer_cidrs=("127.0.0.1/32",),
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",), preview_origin="https://preview.example",
        preview_port=443, preview_allowed_origins=("https://preview.example",), artifact_provider="s3",
        build_provider="codebuild", runtime_provider="isolated-gateway",
        codebuild_image_digest="a" * 64, codebuild_wrapper_sha256="b" * 64,
        codebuild_probe_version="probe-v1", codebuild_account_id="111122223333", codebuild_region="eu-west-1",
    )
    app = create_app(config, providers=_isolated_providers())
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        app.state.project_creation_diagnostics = [{"slug": "secret-project", "operation_id": "op_secret", "status": "diagnostic"}]
        app.state.project_bootstrap_diagnostics = [{"name": "half-secret", "status": "incomplete", "first_seen_at": "one", "last_seen_at": "two"}]
        app.state.preview_recovery_operations = ["pop_secret"]
        app.state.workspace_recovery_diagnostics = [{"journal_id": "save_secret", "project_slug": "secret-project", "error_code": "workspace_recovery_failed"}]
        app.state.submission_recovery_status = {"status": "ok", "error_code": None}
        public = client.get("/api/ready")
        assert public.status_code == 503
        assert public.json() == {
            "status": "degraded",
            "project_creation_diagnostics": {"count": 1, "codes": ["diagnostic"]},
            "project_bootstrap_diagnostics": {"count": 1, "codes": ["incomplete"]},
            "workspace_recovery": {"count": 1, "codes": ["workspace_recovery_failed"]},
            "preview_recovery_operations": {"count": 1, "codes": ["preview_recovery_pending"]},
        }
        assert all(secret not in public.text for secret in ("secret-project", "op_secret", "save_secret", "pop_secret", "half-secret"))
        assert client.get("/api/ready/details").status_code == 401
        detail = client.get(
            "/api/ready/details",
            headers=_signed_headers("secret", nonce="ready-admin", path="/api/ready/details", roles="admin"),
        )
        assert detail.status_code == 503
        assert detail.json()["project_creation_diagnostics"] == [{"slug": "secret-project", "operation_id": "op_secret", "status": "diagnostic"}]
        assert detail.json()["preview_recovery_operations"] == ["pop_secret"]
    connection = connect(config.database_path)
    audit = connection.execute(
        "SELECT event_type,actor_id,data_json FROM admin_audit_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    connection.close()
    assert dict(audit) == {"event_type": "readiness_details_viewed", "actor_id": "alice", "data_json": '{"status":"degraded"}'}


def test_cors_wraps_early_authentication_and_body_limit_errors(tmp_path):
    local_origin = "http://127.0.0.1:5173"
    local = StudioConfig(database_path=tmp_path / "local-cors.sqlite3")
    with TestClient(create_app(local)) as client:
        unauthorized = client.get("/api/projects", headers={"Origin": local_origin})
        assert unauthorized.status_code == 401
        assert unauthorized.headers["access-control-allow-origin"] == local_origin
        client.get("/api/session")
        forbidden = client.post(
            "/api/projects",
            content=b'{"name":"Demo"}',
            headers={"Origin": local_origin, "Content-Type": "application/json"},
        )
        assert forbidden.status_code == 403
        assert forbidden.headers["access-control-allow-origin"] == local_origin
        too_large = client.post(
            "/api/submissions",
            content=b"x" * (local.request_body_limit_bytes + 1),
            headers={"Origin": local_origin, "Content-Type": "application/json"},
        )
        assert too_large.status_code == 413
        assert too_large.headers["access-control-allow-origin"] == local_origin
        preflight = client.options(
            "/api/submissions",
            headers={
                "Origin": local_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-csrf-token,idempotency-key",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == local_origin

    shared_origin = "https://studio.example"
    shared = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "shared-cors.sqlite3",
        proxy_hmac_secret="secret", trusted_peer_cidrs=("127.0.0.1/32",),
        public_base_url=shared_origin, review_base_url=shared_origin,
        cors_origins=(shared_origin,), preview_origin="https://preview.example",
        preview_port=443, preview_allowed_origins=("https://preview.example",), artifact_provider="s3",
        build_provider="codebuild", runtime_provider="isolated-gateway",
        codebuild_image_digest="a" * 64, codebuild_wrapper_sha256="b" * 64,
        codebuild_probe_version="probe-v1", codebuild_account_id="111122223333", codebuild_region="eu-west-1",
    )
    with TestClient(create_app(shared, providers=_isolated_providers()), client=("127.0.0.1", 50000)) as client:
        unauthorized = client.get("/api/projects", headers={"Origin": shared_origin})
        assert unauthorized.status_code == 401
        assert unauthorized.headers["access-control-allow-origin"] == shared_origin
        body = b'{"expected_version":1}'
        timestamp = str(time.time())
        nonce = "cors-forbidden"
        path = "/api/projects/demo-ping/claim"
        signed = {
            "Origin": shared_origin,
            "Content-Type": "application/json",
            "x-studio-identity": "alice",
            "x-studio-roles": "submitter",
            "x-studio-timestamp": timestamp,
            "x-studio-nonce": nonce,
            "x-studio-request-id": "req-" + nonce,
            "x-studio-signature": proxy_signature(
                "secret", "alice", timestamp, nonce, "POST", path, query="",
                body_sha256=hashlib.sha256(body).hexdigest(), roles="submitter", request_id="req-" + nonce,
            ),
        }
        forbidden = client.post(path, content=body, headers=signed)
        assert forbidden.status_code == 403
        assert forbidden.headers["access-control-allow-origin"] == shared_origin
        too_large = client.post(
            "/api/submissions",
            content=b"x" * (shared.request_body_limit_bytes + 1),
            headers={"Origin": shared_origin, "Content-Type": "application/json"},
        )
        assert too_large.status_code == 413
        assert too_large.headers["access-control-allow-origin"] == shared_origin
        preflight = client.options(
            "/api/submissions",
            headers={"Origin": shared_origin, "Access-Control-Request-Method": "POST"},
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == shared_origin


def test_membership_and_claim_validate_canonical_path_identities(tmp_path):
    config = StudioConfig(database_path=tmp_path / "membership.sqlite3")
    with TestClient(create_app(config)) as client:
        client.get("/api/session")
        headers = {"x-csrf-token": client.cookies.get("studio_csrf")}
        for identity in ("%20alice%20", "%01alice", "%EF%BD%81lice"):
            response = client.put(
                f"/api/projects/demo-ping/members/{identity}",
                json={"active": True, "expected_version": 0},
                headers=headers,
            )
            assert response.status_code == 422
            assert response.json()["error"] == "invalid_identity"
        invalid_member_slug = client.put(
            "/api/projects/Bad/members/alice",
            json={"active": True, "expected_version": 0},
            headers=headers,
        )
        assert invalid_member_slug.status_code == 422
        assert invalid_member_slug.json()["error"] == "invalid_project_slug"
        invalid_claim_slug = client.post(
            "/api/projects/Bad/claim",
            json={"expected_version": 1},
            headers=headers,
        )
        assert invalid_claim_slug.status_code == 422
        assert invalid_claim_slug.json()["error"] == "invalid_project_slug"
        valid = client.put(
            "/api/projects/demo-ping/members/alice",
            json={"active": True, "expected_version": 0},
            headers=headers,
        )
        assert valid.status_code == 200
        assert valid.json()["user_id"] == "alice"

    connection = connect(config.database_path)
    rows = connection.execute("SELECT user_id FROM project_memberships ORDER BY created_at").fetchall()
    connection.close()
    assert [row["user_id"] for row in rows] == ["alice"]
