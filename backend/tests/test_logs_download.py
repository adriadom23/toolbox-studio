from __future__ import annotations

import base64
import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.asgi import create_app
from app.core.studio_config import StudioConfig
from app.core.security import encode_cursor
from app.submissions.build import BuildFailed, LocalSubprocessBuildProvider
from app.submissions.logs import LocalLogStore, LogExportDispatcher
from app.submissions.providers import BuildOutputs


def test_logs_are_redacted_and_download_matches_hash(tmp_path, monkeypatch):
    phases = ({"phase": "frontend", "stdout": "token=secret-value\n", "stderr": ""},)
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", phases))
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "studio.sqlite3"))) as client:
        client.get("/api/session")
        headers = {"x-csrf-token": client.cookies.get("studio_csrf")}
        response = client.post("/api/projects/demo-ping/submit", content=b"", headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result["notified"] == {"email": False, "slack": False}
        assert result["meta"]["name"] == "demo-ping"
        assert result["submission"]["submission_id"] == result["submission_id"]
        submission = result["submission_id"]
        logs = client.get(f"/api/submissions/{submission}/logs").json()
        assert "secret-value" not in "".join(logs["lines"])
        for offset in (-1, 10**9):
            cursor = base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")
            invalid = client.get(f"/api/submissions/{submission}/logs", params={"cursor": cursor})
            assert invalid.status_code == 422
            assert invalid.json()["error"] == "invalid_cursor"
        artifact = client.get(result["package_path"])
        assert artifact.status_code == 200
        assert hashlib.sha256(artifact.content).hexdigest() == result["artifact_sha256"]
        assert "C:" not in result["package_path"]


def test_failed_build_persists_redacted_phase_logs(tmp_path, monkeypatch):
    phases = ({"phase": "frontend", "stdout": "token=secret-value\n", "stderr": "vite failed\n"},)

    def fail(_self, _request):
        raise BuildFailed("vite_build_failed", phases=phases)

    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", fail)
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "failed.sqlite3"))) as client:
        client.get("/api/session")
        headers = {"x-csrf-token": client.cookies.get("studio_csrf")}
        response = client.post("/api/projects/demo-ping/submit", json={}, headers=headers)
        assert response.status_code == 422
        result = response.json()
        assert result["status"] == "build_failed"
        assert result["error"] == "package_failed"
        assert result["error_code"] == "vite_build_failed"
        assert result["notified"] == {"email": False, "slack": False}
        assert result["submission"]["state"] == "build_failed"
        logs = client.get(f"/api/submissions/{result['submission_id']}/logs", params={"phase": "frontend"}).json()
        assert "vite failed" in "".join(logs["lines"])
        assert "secret-value" not in "".join(logs["lines"])
        assert any(phase["phase"] == "build" and phase["status"] == "failed" for phase in result["phases"])


def test_log_redaction_covers_cloud_credentials_json_and_chunk_boundaries(tmp_path):
    store = LocalLogStore(tmp_path / "logs", max_phase_bytes=4096)
    canaries = (
        "aws-secret-canary",
        "aws-session-canary",
        "json-token-canary",
        "authorization-canary",
        "split-secret-canary",
    )
    store.append("sub_test", "build", "AWS_SECRET_ACCESS_KEY=aws-secret-canary\nAWS_SESSION_TOKEN = 'aws-session-canary'\n")
    store.append("sub_test", "build", '{"token":"json-token-canary"}\nAuthorization: Bearer authorization-canary\nAWS_SECRET_')
    store.append("sub_test", "build", "ACCESS_KEY=split-secret-canary\n")
    page = store.read_page("sub_test", "build", None, 100, 4096)
    content = "\n".join(page.lines)
    assert "[REDACTED]" in content
    assert all(canary not in content for canary in canaries)

    class Exporter:
        def append(self, _ref, _phase, value):
            self.value = value

    exporter = Exporter()
    dispatcher = LogExportDispatcher(tmp_path / "unused.sqlite3", store, exporter)
    dispatcher._export_bounded("sub_test", "build", dispatcher._read_spool("sub_test", "build"))
    assert all(canary not in exporter.value for canary in canaries)


def test_log_cursors_are_signed_scoped_and_expiring(tmp_path, monkeypatch):
    phases = (
        {"phase": "frontend", "stdout": "one\ntwo\nthree\n", "stderr": ""},
        {"phase": "backend", "stdout": "backend-one\nbackend-two\n", "stderr": ""},
    )
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", phases))
    config = StudioConfig(database_path=tmp_path / "cursors.sqlite3")
    with TestClient(create_app(config)) as client:
        client.get("/api/session")
        headers = {"x-csrf-token": client.cookies.get("studio_csrf")}
        submitted = client.post("/api/projects/demo-ping/submit", content=b"", headers=headers).json()
        submission_id = submitted["submission_id"]
        first = client.get(
            f"/api/submissions/{submission_id}/logs",
            params={"phase": "frontend", "line_limit": 1},
        ).json()
        assert first["has_more"] is True
        assert first["truncated"] is False
        cursor = first["next_cursor"]
        assert cursor and cursor != base64.urlsafe_b64encode(b"4").decode("ascii").rstrip("=")

        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        assert client.get(
            f"/api/submissions/{submission_id}/logs",
            params={"phase": "frontend", "cursor": tampered},
        ).status_code == 422
        assert client.get(
            f"/api/submissions/{submission_id}/logs",
            params={"phase": "backend", "cursor": cursor},
        ).status_code == 422

        issued = int(datetime.now(timezone.utc).timestamp())
        wrong_submission = encode_cursor(config.cursor_hmac_secret, {
            "kind": "log", "actor": "local-user", "submission_id": "sub_" + "f" * 32,
            "phase": "frontend", "provider_cursor": "MA", "iat": issued, "exp": issued + 60,
        })
        expired = encode_cursor(config.cursor_hmac_secret, {
            "kind": "log", "actor": "local-user", "submission_id": submission_id,
            "phase": "frontend", "provider_cursor": "MA", "iat": issued - 120, "exp": issued - 60,
        })
        for invalid_cursor in (wrong_submission, expired):
            response = client.get(
                f"/api/submissions/{submission_id}/logs",
                params={"phase": "frontend", "cursor": invalid_cursor},
            )
            assert response.status_code == 422
            assert response.json()["error"] == "invalid_cursor"

        final = client.get(
            f"/api/submissions/{submission_id}/logs",
            params={"phase": "frontend", "cursor": cursor, "line_limit": 500},
        ).json()
        assert final["has_more"] is False
        assert final["truncated"] is False


def test_physical_log_truncation_is_durable_on_last_page(tmp_path, monkeypatch):
    phases = ({"phase": "frontend", "stdout": "first-line\n" + "x" * 200, "stderr": ""},)
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", phases))
    config = StudioConfig(database_path=tmp_path / "truncated.sqlite3", log_phase_max_bytes=32)
    with TestClient(create_app(config)) as client:
        client.get("/api/session")
        submitted = client.post(
            "/api/projects/demo-ping/submit",
            content=b"",
            headers={"x-csrf-token": client.cookies.get("studio_csrf")},
        ).json()
        page = client.get(
            f"/api/submissions/{submitted['submission_id']}/logs",
            params={"phase": "frontend", "line_limit": 500},
        ).json()
        assert page["next_cursor"] is None
        assert page["has_more"] is False
        assert page["truncated"] is True


def test_legacy_submit_async_and_completed_replay_keep_compatible_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", ()))
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "async.sqlite3"))) as client:
        client.get("/api/session")
        headers = {
            "x-csrf-token": client.cookies.get("studio_csrf"),
            "Idempotency-Key": "legacy-contract-key",
            "Prefer": "respond-async",
        }
        response = client.post("/api/projects/demo-ping/submit", json={}, headers=headers)
        assert response.status_code == 202
        initial = response.json()
        assert initial["status"] == "building"
        assert initial["package_path"] is None
        assert initial["notified"] == {"email": False, "slack": False}
        assert initial["meta"]["name"] == "demo-ping"
        assert initial["submission"]["submission_id"] == initial["submission_id"]

        terminal = None
        for _ in range(100):
            terminal = client.get(initial["poll_url"]).json()
            notify_phase = next(phase for phase in terminal["phases"] if phase["phase"] == "notify")
            notifications_terminal = all(
                notification["status"] not in {"pending", "sending", "running"}
                for notification in terminal["notifications"]
            )
            if terminal["status"] != "building" and notify_phase["status"] not in {"pending", "running"} and notifications_terminal:
                break
            time.sleep(0.02)
        assert terminal["status"] == "submitted"
        replay = client.post(
            "/api/projects/demo-ping/submit",
            json={},
            headers={key: value for key, value in headers.items() if key != "Prefer"},
        )
        assert replay.status_code == 200
        assert replay.json() == terminal
        assert replay.json()["notified"] == {"email": False, "slack": False}
