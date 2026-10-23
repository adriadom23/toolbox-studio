from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.api.routers.submissions import _row_dict
from app.asgi import create_app
from app.core.studio_config import StudioConfig
from app.submissions.build import LocalSubprocessBuildProvider
from app.submissions.database import connect, transaction
from app.submissions.repository_v2 import PipelineRepository
from app.submissions.providers import BuildOutputs


def csrf(client): return {"x-csrf-token": client.cookies.get("studio_csrf")}


def test_create_poll_list_and_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", ()))
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "studio.sqlite3"))) as client:
        client.get("/api/session")
        context = client.get("/api/projects/demo-ping/submission-context")
        assert context.status_code == 200
        assert context.json()["providers"] == {
            "artifact": "local",
            "build": "local-subprocess",
            "logs": "local",
            "notifications": ["local"],
            "publication": "local",
            "runtime": "local-subprocess",
        }
        response = client.post("/api/submissions", json={"project_slug": "demo-ping", "title": "Demo", "description": "Ready"}, headers={**csrf(client), "Idempotency-Key": "api-test-key"})
        assert response.status_code == 202
        submission_id = response.json()["submission_id"]
        detail = client.get(f"/api/submissions/{submission_id}")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert not {"artifact_ref", "manifest_json", "git_json", "snapshot_ref", "build_output_ref"} & set(detail_payload)
        assert str(tmp_path) not in json.dumps(detail_payload)
        listing = client.get("/api/submissions", params={"page_size": 100}).json()
        assert listing["total"] >= 1
        assert any(item["id"] == submission_id for item in listing["items"])
        assert all(not {"artifact_ref", "manifest_json", "git_json", "snapshot_ref", "build_output_ref"} & set(item) for item in listing["items"])
        connection = connect(tmp_path / "studio.sqlite3")
        refs = connection.execute("SELECT snapshot_ref,build_output_ref FROM submission_records WHERE id=?", (submission_id,)).fetchone()
        connection.close()
        assert refs["snapshot_ref"].startswith("runtime:v1:") and refs["build_output_ref"].startswith("runtime:v1:")
        assert not Path(refs["snapshot_ref"]).is_absolute() and not Path(refs["build_output_ref"]).is_absolute()
        assert client.get(f"/api/submissions/{submission_id}/manifest").status_code == 200


def test_build_reserved_audits_effective_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", ()))
    config = StudioConfig(
        database_path=tmp_path / "roles.sqlite3",
        identity_roles={"local-user": ("reviewer", "admin")},
    )
    with TestClient(create_app(config)) as client:
        client.get("/api/session")
        response = client.post(
            "/api/submissions",
            json={"project_slug": "demo-ping", "title": "Admin", "description": "Role audit"},
            headers={**csrf(client), "Idempotency-Key": "effective-role-key"},
        )
        assert response.status_code == 202
        connection = connect(config.database_path)
        event = connection.execute(
            "SELECT actor_roles_json FROM submission_operation_events WHERE submission_id=? AND event_type='build_reserved'",
            (response.json()["submission_id"],),
        ).fetchone()
        connection.close()
        assert json.loads(event["actor_roles_json"]) == ["admin", "reviewer"]


def test_configured_local_actor_drives_session_claim_submission_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", ()))
    config = StudioConfig(
        database_path=tmp_path / "configured-actor.sqlite3",
        local_user_id="local-owner",
        local_display_name="Propietaria local",
        identity_roles={"local-owner": ("admin", "reviewer", "submitter")},
    )
    with TestClient(create_app(config)) as client:
        session = client.get("/api/session")
        assert session.status_code == 200
        assert session.json()["identity"] == "local-owner"
        assert session.json()["display_name"] == "Propietaria local"
        assert session.json()["roles"] == ["admin", "reviewer", "submitter"]

        context = client.get("/api/projects/demo-ping/submission-context")
        assert context.status_code == 200
        assert context.json()["actor"] == {"id": "local-owner", "display_name": "Propietaria local"}
        headers = {**csrf(client), "Idempotency-Key": "configured-local-actor"}
        spoofed = client.post(
            "/api/submissions",
            json={
                "project_slug": "demo-ping",
                "title": "Actor local",
                "description": "No acepta autor del cliente",
                "author_id": "attacker",
            },
            headers={**csrf(client), "Idempotency-Key": "spoofed-local-actor"},
        )
        assert spoofed.status_code == 422
        response = client.post(
            "/api/submissions",
            json={"project_slug": "demo-ping", "title": "Actor local", "description": "Identidad configurada"},
            headers=headers,
        )
        assert response.status_code == 202
        submission_id = response.json()["submission_id"]
        detail = client.get(f"/api/submissions/{submission_id}")
        assert detail.status_code == 200
        assert detail.json()["author_id"] == "local-owner"
        assert detail.json()["author_display"] == "Propietaria local"

    connection = connect(config.database_path)
    owner = connection.execute("SELECT owner_id FROM projects WHERE slug='demo-ping'").fetchone()
    claim = connection.execute(
        "SELECT actor_id FROM project_security_events WHERE project_slug='demo-ping' AND event_type='project_claimed_local'"
    ).fetchone()
    reservation = connection.execute(
        "SELECT actor_id FROM idempotency_records WHERE operation_id=?",
        (response.json()["operation_id"],),
    ).fetchone()
    event = connection.execute(
        "SELECT actor_id,actor_roles_json FROM submission_operation_events WHERE submission_id=? AND event_type='build_reserved'",
        (submission_id,),
    ).fetchone()
    connection.close()
    assert owner["owner_id"] == "local-owner"
    assert claim["actor_id"] == "local-owner"
    assert reservation["actor_id"] == "local-owner"
    assert event["actor_id"] == "local-owner"
    assert json.loads(event["actor_roles_json"]) == ["admin", "reviewer", "submitter"]


def test_submission_public_mapping_is_closed_to_future_database_columns():
    row = {
        "id": "sub_" + "a" * 32,
        "project_slug": "demo",
        "state": "building",
        "artifact_ref": None,
        "snapshot_ref": r"C:\\runtime\\secret",
        "build_output_ref": r"C:\\runtime\\output",
        "future_internal_ref": r"C:\\future\\secret",
    }
    payload = _row_dict(row)
    assert payload["id"] == row["id"]
    assert not {"snapshot_ref", "build_output_ref", "future_internal_ref"} & set(payload)
    assert "C:\\" not in json.dumps(payload)


def test_submission_admission_returns_507_before_build_when_storage_is_insufficient(tmp_path):
    config = StudioConfig(database_path=tmp_path / "capacity.sqlite3", storage_min_free_bytes=10 ** 18)
    with TestClient(create_app(config)) as client:
        client.get("/api/session")
        response = client.post(
            "/api/submissions",
            json={"project_slug": "demo-ping", "title": "Demo", "description": "Ready"},
            headers={**csrf(client), "Idempotency-Key": "storage-capacity-key"},
        )
        assert response.status_code == 507
        connection = connect(config.database_path)
        assert connection.execute("SELECT COUNT(*) FROM submission_records").fetchone()[0] == 0
        connection.close()


def test_manifest_is_paginated_with_actor_bound_cursor(tmp_path):
    database = tmp_path / "manifest.sqlite3"
    with TestClient(create_app(StudioConfig(database_path=database))) as client:
        client.get("/api/session")
        files = [{"path": f"frontend/demo/file-{index}.txt", "kind": "frontend", "size": index, "sha256": f"{index:064x}"} for index in range(1001)]
        connection = connect(database)
        with transaction(connection):
            now = "2026-01-01T00:00:00+00:00"
            connection.execute(
                "INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,artifact_ref,artifact_sha256,artifact_size,manifest_json,created_at,updated_at) "
                "VALUES ('sub_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','demo-ping',1,'v1','submitted','alice','Alice','T','D','ref',?,1,?,?,?)",
                ("a" * 64, json.dumps({"format": 2, "files": files}), now, now),
            )
        connection.close()
        first = client.get("/api/submissions/sub_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/manifest", params={"page_size": 1000})
        assert first.status_code == 200
        assert len(first.json()["files"]) == 1000 and first.json()["total"] == 1001
        cursor = first.json()["next_cursor"]
        second = client.get("/api/submissions/sub_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/manifest", params={"page_size": 1000, "cursor": cursor})
        assert len(second.json()["files"]) == 1 and second.json()["next_cursor"] is None
        assert client.get("/api/submissions/sub_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/manifest", params={"cursor": "tampered"}).status_code == 422


def test_list_caps_pages_at_one_hundred_and_signs_contextual_cursor(tmp_path):
    database = tmp_path / "many.sqlite3"
    with TestClient(create_app(StudioConfig(database_path=database))) as client:
        client.get("/api/session")
        connection = connect(database)
        with transaction(connection):
            repo = PipelineRepository(connection)
            for index in range(101):
                repo.insert_building(submission_id=f"sub_{index:032x}", slug="demo-ping", version=index + 1, actor="alice", title="T", description="D", correlation_id=None)
        connection.close()
        assert client.get("/api/submissions", params={"page_size": 101}).status_code == 422
        first = client.get("/api/submissions", params={"page_size": 100, "sort": "id_asc"})
        assert first.status_code == 200 and len(first.json()["items"]) == 100
        assert first.json()["total"] == 101
        cursor = first.json()["next_cursor"]
        second = client.get("/api/submissions", params={"page_size": 100, "sort": "id_asc", "cursor": cursor})
        assert second.status_code == 200 and len(second.json()["items"]) == 1
        assert client.get("/api/submissions", params={"cursor": cursor, "status": "submitted"}).status_code == 422
        assert client.get("/api/submissions", params={"cursor": "tampered"}).status_code == 422
        assert client.get("/api/submissions", params={"status": "not-a-state"}).status_code == 422
        assert client.get("/api/submissions", params={"created_from": "2026-01-01"}).status_code == 422
        assert client.get("/api/submissions", params={"created_from": "2026-02-01T00:00:00Z", "created_to": "2026-01-01T00:00:00Z"}).status_code == 422
