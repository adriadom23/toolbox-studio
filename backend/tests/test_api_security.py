from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.asgi import BodyLimitMiddleware, create_app
from app.core.studio_config import StudioConfig
from app.projects import registry


def test_submission_mutations_require_csrf_and_reject_extra_fields(tmp_path):
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "db.sqlite3"))) as client:
        client.get("/api/session")
        body = {"project_slug": "demo-ping", "description": "x", "unexpected": True}
        assert client.post("/api/submissions", json=body, headers={"Idempotency-Key": "abcdefgh"}).status_code == 403
        headers = {"Idempotency-Key": "abcdefgh", "x-csrf-token": client.cookies.get("studio_csrf")}
        assert client.post("/api/submissions", json=body, headers=headers).status_code == 422
        whitespace = client.post(
            "/api/submissions",
            json={"project_slug": "demo-ping", "title": "   ", "description": "   "},
            headers={**headers, "Idempotency-Key": "whitespace-key"},
        )
        assert whitespace.status_code == 422
        assert client.get("/api/submissions/not-an-id").status_code == 422


def test_body_limit_rejects_declared_and_chunked_payloads_before_routing(tmp_path):
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "limits.sqlite3"))) as client:
        declared = client.post(
            "/api/submissions",
            content=b"x" * (2 * 1024 * 1024 + 1),
            headers={"content-type": "application/json", "x-correlation-id": "too-large"},
        )
        assert declared.status_code == 413
        assert declared.json()["correlation_id"] == "too-large"

    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    chunks = [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56", "more_body": False},
    ]
    sent = []

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(BodyLimitMiddleware(downstream, default_limit=5)(
        {"type": "http", "path": "/api/submissions", "headers": [], "state": {"correlation_id": "chunked"}},
        receive,
        send,
    ))
    assert called is False
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["correlation_id"] == "chunked"


def test_public_error_envelope_covers_validation_media_json_and_internal(tmp_path, monkeypatch):
    app = create_app(StudioConfig(database_path=tmp_path / "errors.sqlite3"))
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/api/session")
        csrf = client.cookies.get("studio_csrf")
        headers = {"x-csrf-token": csrf, "Idempotency-Key": "abcdefgh", "x-correlation-id": "corr-test"}
        validation = client.post("/api/submissions", json={"project_slug": "demo-ping"}, headers=headers)
        assert validation.status_code == 422
        malformed = client.post("/api/submissions", content="{", headers={**headers, "content-type": "application/json"})
        assert malformed.status_code == 422 and malformed.json()["error"] == "invalid_json"
        media = client.post("/api/submissions", content="text", headers={**headers, "content-type": "text/plain"})
        assert media.status_code == 415
        for response in (validation, malformed, media):
            assert set(("error", "message", "recoverable", "correlation_id")) <= set(response.json())
            assert response.json()["correlation_id"] == "corr-test"

        monkeypatch.setattr(registry, "list_projects", lambda: (_ for _ in ()).throw(RuntimeError("secret C:/private")))
        internal = client.get("/api/projects", headers={"x-correlation-id": "corr-internal"})
        assert internal.status_code == 500
        assert internal.json() == {"error": "internal_error", "message": "an internal error occurred", "recoverable": True, "correlation_id": "corr-internal"}
        assert "private" not in internal.text
