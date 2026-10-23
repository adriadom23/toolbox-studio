from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import app.asgi as asgi_module  # noqa: E402
from app.asgi import create_app  # noqa: E402
from app.core.errors import PublicError  # noqa: E402
from app.core.studio_config import StudioConfig  # noqa: E402
from app.projects import registry, scaffold  # noqa: E402
from app.projects.safe_fs import stable_directories, stable_file_target  # noqa: E402
from app.submissions.database import connect, transaction  # noqa: E402
from app.submissions.migrations import migrate  # noqa: E402
from app.submissions.snapshot import SafeSnapshotBuilder  # noqa: E402


@pytest.fixture()
def project_client(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    frontend.mkdir()
    backend.mkdir()
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend)
    monkeypatch.setattr(scaffold, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(scaffold, "BACKEND_PROJECTS_DIR", backend)
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "studio.sqlite3"))) as client:
        client.get("/api/session")
        yield client


def _wait_ready(client, timeout=2):
    deadline = time.monotonic() + timeout
    response = client.get("/api/ready")
    while response.status_code != 200 and time.monotonic() < deadline:
        time.sleep(0.01)
        response = client.get("/api/ready")
    return response


def _csrf(client, key="project-test-key"):
    return {"x-csrf-token": client.cookies.get("studio_csrf"), "Idempotency-Key": key}


def test_project_crud_without_delete(project_client):
    response = project_client.post("/api/projects", json={"name": "My Tool"}, headers=_csrf(project_client))
    assert response.status_code == 201
    assert response.json()["name"] == "my-tool"
    replay = project_client.post("/api/projects", json={"name": "My Tool"}, headers=_csrf(project_client))
    assert replay.status_code == 201 and replay.json() == response.json()
    assert project_client.post("/api/projects", json={"name": "My Tool", "extra": True}, headers=_csrf(project_client, "project-extra-key")).status_code == 422
    connection = connect(project_client.app.state.config.database_path)
    reservation = connection.execute("SELECT status FROM project_create_reservations WHERE slug='my-tool'").fetchone()
    connection.close()
    assert reservation["status"] == "completed"
    assert project_client.get("/api/projects").json()[0]["name"] == "my-tool"
    detail = project_client.get("/api/projects/my-tool").json()
    assert detail["meta"]["name"] == "my-tool"
    assert project_client.delete("/api/projects/my-tool", headers=_csrf(project_client)).status_code == 404


def test_file_save_rejects_traversal_before_writes(project_client):
    project_client.post("/api/projects", json={"name": "Safe Tool"}, headers=_csrf(project_client))
    detail = project_client.get("/api/projects/safe-tool").json()
    current = detail["files"][0]
    response = project_client.put("/api/projects/safe-tool/files", json={"files": [{"path": "frontend/../../escape.txt", "content": "bad", "base_sha256": current["sha256"]}]}, headers=_csrf(project_client))
    assert response.status_code == 422


def test_file_save_is_closed_cas_guarded_and_rejects_reserved_paths(project_client):
    project_client.post("/api/projects", json={"name": "CAS Tool"}, headers=_csrf(project_client))
    detail = project_client.get("/api/projects/cas-tool").json()
    component = next(item for item in detail["files"] if item["path"].endswith(".vue"))
    endpoint = "/api/projects/cas-tool/files"
    assert project_client.put(endpoint, json={"files": [{"path": component["path"], "content": "changed", "base_sha256": "0" * 64}]}, headers=_csrf(project_client)).status_code == 409
    assert project_client.put(endpoint, json={"files": [{"path": "frontend/project.meta.json", "content": "{}", "base_sha256": None}]}, headers=_csrf(project_client)).status_code == 422
    assert project_client.put(endpoint, json={"files": [{"path": component["path"], "content": "changed", "base_sha256": component["sha256"], "extra": True}]}, headers=_csrf(project_client)).status_code == 422
    saved = project_client.put(endpoint, json={"files": [{"path": component["path"], "content": "changed", "base_sha256": component["sha256"]}]}, headers=_csrf(project_client))
    assert saved.status_code == 200, saved.text
    assert next(item for item in project_client.get("/api/projects/cas-tool").json()["files"] if item["path"] == component["path"])["content"] == "changed"


def test_project_file_limits_accept_two_mib_files_and_ten_mib_batches(project_client):
    project_client.post("/api/projects", json={"name": "Large Files"}, headers=_csrf(project_client, "large-create"))
    content = "x" * (2 * 1024 * 1024)
    files = [
        {"path": f"frontend/large-{index}.txt", "content": content, "base_sha256": None}
        for index in range(5)
    ]
    saved = project_client.put(
        "/api/projects/large-files/files",
        json={"files": files},
        headers=_csrf(project_client, "large-save"),
    )
    assert saved.status_code == 200, saved.text
    for index in range(5):
        assert (registry.frontend_project_dir("large-files") / f"large-{index}.txt").stat().st_size == 2 * 1024 * 1024
    detail = project_client.get("/api/projects/large-files").json()
    item = next(file for file in detail["files"] if file["path"] == "frontend/large-0.txt")
    assert item["type"] == "editable" and item["size"] == 2 * 1024 * 1024


def test_opaque_files_are_visible_and_preserved_when_text_is_saved(project_client):
    project_client.post("/api/projects", json={"name": "Opaque Tool"}, headers=_csrf(project_client, "opaque-create"))
    binary = registry.frontend_project_dir("opaque-tool") / "image.bin"
    original = b"\x00\xfftoolbox\x80"
    binary.write_bytes(original)
    detail = project_client.get("/api/projects/opaque-tool").json()
    opaque = next(item for item in detail["files"] if item["path"] == "frontend/image.bin")
    assert opaque == {
        "path": "frontend/image.bin",
        "type": "opaque",
        "size": len(original),
        "sha256": hashlib.sha256(original).hexdigest(),
    }
    component = next(item for item in detail["files"] if item["path"].endswith(".vue"))
    saved = project_client.put(
        "/api/projects/opaque-tool/files",
        json={"files": [{"path": component["path"], "content": "changed", "base_sha256": component["sha256"]}]},
        headers=_csrf(project_client, "opaque-save"),
    )
    assert saved.status_code == 200
    assert binary.read_bytes() == original


def test_save_updates_sqlite_metadata_without_post_promotion_filesystem_write(project_client, monkeypatch):
    project_client.post("/api/projects", json={"name": "Metadata Tool"}, headers=_csrf(project_client, "metadata-create"))
    detail = project_client.get("/api/projects/metadata-tool").json()
    component = next(item for item in detail["files"] if item["path"].endswith(".vue"))
    monkeypatch.setattr(registry, "touch_modified", lambda _slug: (_ for _ in ()).throw(AssertionError("legacy metadata must not be touched")))
    saved = project_client.put(
        "/api/projects/metadata-tool/files",
        json={"files": [{"path": component["path"], "content": "changed", "base_sha256": component["sha256"]}]},
        headers=_csrf(project_client, "metadata-save"),
    )
    assert saved.status_code == 200
    assert saved.json()["modified_at"]
    assert project_client.get("/api/projects/metadata-tool").json()["meta"]["modified_at"] == saved.json()["modified_at"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_project_root_junction_is_rejected_and_open_guard_blocks_replacement(tmp_path, monkeypatch):
    frontend_projects = tmp_path / "frontend"
    backend_projects = tmp_path / "backend"
    frontend_project = frontend_projects / "junction-tool"
    backend_project = backend_projects / "junction-tool"
    outside = tmp_path / "outside"
    frontend_project.mkdir(parents=True)
    backend_project.mkdir(parents=True)
    outside.mkdir()
    (frontend_project / "JunctionTool.vue").write_text("safe", encoding="utf-8")
    (backend_project / "routes.py").write_text("safe", encoding="utf-8")
    (outside / "Outside.vue").write_text("external-secret", encoding="utf-8")
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend_projects)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend_projects)

    with stable_directories(frontend_project):
        with pytest.raises(OSError):
            os.replace(frontend_project, frontend_projects / "guarded-original")

    original = frontend_projects / "junction-tool-original"
    os.replace(frontend_project, original)
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(frontend_project), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    assert not registry.project_exists("junction-tool")
    with pytest.raises(PublicError) as rejected:
        SafeSnapshotBuilder(frontend_projects, backend_projects).build("junction-tool", tmp_path / "snapshot")
    assert rejected.value.code == "not_found"
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows descendant junction regression")
def test_project_read_rejects_descendant_junction_without_exposing_external_content(project_client):
    project_client.post("/api/projects", json={"name": "Read Junction"}, headers=_csrf(project_client, "read-junction-create"))
    outside = registry.FRONTEND_PROJECTS_DIR.parent / "outside-read-junction"
    outside.mkdir()
    canary = "external-junction-canary"
    (outside / "secret.txt").write_text(canary, encoding="utf-8")
    alias = registry.frontend_project_dir("read-junction") / "alias"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr

    response = project_client.get("/api/projects/read-junction")

    assert response.status_code == 409
    assert response.json()["error"] == "workspace_root_unsafe"
    assert canary not in response.text
    assert "alias/secret.txt" not in response.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX descendant symlink regression")
def test_project_read_rejects_descendant_symlink_without_exposing_external_content(project_client):
    project_client.post("/api/projects", json={"name": "Read Symlink"}, headers=_csrf(project_client, "read-symlink-create"))
    outside = registry.FRONTEND_PROJECTS_DIR.parent / "outside-read-symlink"
    outside.mkdir()
    canary = "external-symlink-canary"
    (outside / "secret.txt").write_text(canary, encoding="utf-8")
    alias = registry.frontend_project_dir("read-symlink") / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    response = project_client.get("/api/projects/read-symlink")

    assert response.status_code == 409
    assert response.json()["error"] == "workspace_root_unsafe"
    assert canary not in response.text
    assert "alias/secret.txt" not in response.text


def test_file_save_rejects_descendant_directory_links(project_client):
    project_client.post("/api/projects", json={"name": "Linked Child"}, headers=_csrf(project_client, "linked-create"))
    project_root = registry.frontend_project_dir("linked-child")
    real = project_root / "real"
    real.mkdir()
    target = real / "data.txt"
    target.write_text("safe", encoding="utf-8")
    alias = project_root / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks unavailable")
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(real)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
    response = project_client.put(
        "/api/projects/linked-child/files",
        json={"files": [{
            "path": "frontend/alias/data.txt",
            "content": "unsafe",
            "base_sha256": hashlib.sha256(b"safe").hexdigest(),
        }]},
        headers=_csrf(project_client, "linked-save"),
    )
    assert response.status_code == 422
    assert target.read_text(encoding="utf-8") == "safe"


def test_stable_file_target_never_redirects_promotion_after_parent_replacement(tmp_path):
    root = tmp_path / "root"
    parent = root / "parent"
    outside = tmp_path / "outside"
    root.mkdir()
    parent.mkdir()
    outside.mkdir()
    (outside / "data.txt").write_text("outside", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.write_text("promoted", encoding="utf-8")
    moved = root / "parent-original"

    if os.name == "nt":
        with stable_file_target(root, "parent/data.txt"):
            with pytest.raises(OSError):
                os.replace(parent, moved)
        assert (outside / "data.txt").read_text(encoding="utf-8") == "outside"
        return

    with pytest.raises(OSError, match="parent identity changed"):
        with stable_file_target(root, "parent/data.txt") as guarded:
            os.replace(parent, moved)
            parent.symlink_to(outside, target_is_directory=True)
            guarded.replace_from(staged)
    assert (moved / "data.txt").read_text(encoding="utf-8") == "promoted"
    assert (outside / "data.txt").read_text(encoding="utf-8") == "outside"


def test_registry_metadata_reader_does_not_follow_external_link(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    (frontend / "linked-meta").mkdir(parents=True)
    (backend / "linked-meta").mkdir(parents=True)
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend)
    external = tmp_path / "outside.json"
    external.write_text('{"name":"outside","status":"submitted"}', encoding="utf-8")
    linked = frontend / "linked-meta" / "project.meta.json"
    try:
        linked.symlink_to(external)
    except OSError:
        linked.write_text('{"name":"outside","status":"submitted"}', encoding="utf-8")
        monkeypatch.setattr(
            registry,
            "_open_regular_nofollow",
            lambda _path: (_ for _ in ()).throw(OSError("simulated Windows reparse rejection")),
        )
    assert registry.read_meta("linked-meta") == {
        "name": "linked-meta", "displayName": "linked-meta", "created_at": None,
        "modified_at": None, "status": "draft",
    }
    assert external.read_text(encoding="utf-8") == '{"name":"outside","status":"submitted"}'


def test_concurrent_saves_with_the_same_base_have_one_cas_winner(project_client, monkeypatch):
    project_client.post("/api/projects", json={"name": "Concurrent Tool"}, headers=_csrf(project_client, "concurrent-create"))
    detail = project_client.get("/api/projects/concurrent-tool").json()
    component = next(item for item in detail["files"] if item["path"].endswith(".vue"))
    endpoint = "/api/projects/concurrent-tool/files"
    entered = threading.Event()
    release = threading.Event()
    original_persist = asgi_module._persist_workspace_save_state
    blocked = False

    def persist_once(*args, **kwargs):
        nonlocal blocked
        if not blocked:
            blocked = True
            entered.set()
            assert release.wait(2)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(asgi_module, "_persist_workspace_save_state", persist_once)

    def save(content):
        return project_client.put(
            endpoint,
            json={"files": [{"path": component["path"], "content": content, "base_sha256": component["sha256"]}]},
            headers=_csrf(project_client, f"save-{content}"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, "first")
        assert entered.wait(2)
        second = pool.submit(save, "second")
        time.sleep(0.05)
        assert not second.done()
        release.set()
        responses = [first.result(), second.result()]
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    contents = {item["path"]: item["content"] for item in project_client.get("/api/projects/concurrent-tool").json()["files"]}
    assert contents[component["path"]] in {"first", "second"}
    assert winner.status_code == 200


def test_project_read_waits_for_complete_multi_file_promotion(project_client, monkeypatch):
    project_client.post("/api/projects", json={"name": "Atomic Read"}, headers=_csrf(project_client, "atomic-create"))
    detail = project_client.get("/api/projects/atomic-read").json()
    frontend_file = next(item for item in detail["files"] if item["path"].startswith("frontend/") and item["path"].endswith(".vue"))
    backend_file = next(item for item in detail["files"] if item["path"].startswith("backend/") and item["path"].endswith("routes.py"))
    entered = threading.Event()
    release = threading.Event()
    original_replace = asgi_module.os.replace
    blocked = False

    def pause_first_promotion(source, target):
        nonlocal blocked
        result = original_replace(source, target)
        if not blocked and Path(source).name == "0.content":
            blocked = True
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(asgi_module.os, "replace", pause_first_promotion)
    with ThreadPoolExecutor(max_workers=2) as pool:
        save = pool.submit(
            project_client.put,
            "/api/projects/atomic-read/files",
            json={"files": [
                {"path": frontend_file["path"], "content": "frontend-new", "base_sha256": frontend_file["sha256"]},
                {"path": backend_file["path"], "content": "backend-new", "base_sha256": backend_file["sha256"]},
            ]},
            headers=_csrf(project_client, "atomic-save"),
        )
        assert entered.wait(2)
        read = pool.submit(project_client.get, "/api/projects/atomic-read")
        time.sleep(0.05)
        assert not read.done()
        release.set()
        assert save.result().status_code == 200
        response = read.result()
    assert response.status_code == 200
    contents = {item["path"]: item["content"] for item in response.json()["files"]}
    assert contents[frontend_file["path"]] == "frontend-new"
    assert contents[backend_file["path"]] == "backend-new"


def test_file_save_rejects_noncanonical_windows_and_unicode_aliases(project_client):
    project_client.post("/api/projects", json={"name": "Path Tool"}, headers=_csrf(project_client))
    endpoint = "/api/projects/path-tool/files"
    invalid_paths = (
        "frontend\\alias.js",
        "frontend/%2f.js",
        "frontend//empty.js",
        "frontend/./dot.js",
        "frontend/../escape.js",
        "frontend/CON.txt",
        "frontend/trailing.",
    )
    for index, path in enumerate(invalid_paths):
        response = project_client.put(
            endpoint,
            json={"files": [{"path": path, "content": "x", "base_sha256": None}]},
            headers=_csrf(project_client, f"path-key-{index}"),
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_project_path"

    collision = project_client.put(
        endpoint,
        json={"files": [
            {"path": "frontend/K.js", "content": "one", "base_sha256": None},
            {"path": "frontend/Ｋ.js", "content": "two", "base_sha256": None},
        ]},
        headers=_csrf(project_client, "path-collision-key"),
    )
    assert collision.status_code == 422
    assert collision.json()["error"] == "duplicate_project_path"


def test_partial_multi_file_save_blocks_consumers_until_verified_roll_forward(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    frontend.mkdir()
    backend.mkdir()
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend)
    monkeypatch.setattr(scaffold, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(scaffold, "BACKEND_PROJECTS_DIR", backend)
    database = tmp_path / "studio.sqlite3"
    config = StudioConfig(database_path=database)
    original_replace = os.replace

    with TestClient(create_app(config)) as client:
        client.get("/api/session")
        client.post("/api/projects", json={"name": "Recover Tool"}, headers=_csrf(client, "recover-create-key"))
        detail = client.get("/api/projects/recover-tool").json()
        frontend_file = next(item for item in detail["files"] if item["path"].startswith("frontend/") and item["path"].endswith(".vue"))
        backend_file = next(item for item in detail["files"] if item["path"].startswith("backend/") and item["path"].endswith("routes.py"))

        def fail_second_promotion(source, target):
            if Path(source).name == "1.content":
                raise OSError("simulated second promotion failure")
            return original_replace(source, target)

        monkeypatch.setattr("app.asgi.os.replace", fail_second_promotion)
        response = client.put(
            "/api/projects/recover-tool/files",
            json={"files": [
                {"path": frontend_file["path"], "content": "frontend recovered", "base_sha256": frontend_file["sha256"]},
                {"path": backend_file["path"], "content": "backend recovered", "base_sha256": backend_file["sha256"]},
            ]},
            headers=_csrf(client, "recover-save-key"),
        )
        assert response.status_code == 503
        assert response.json()["error"] == "workspace_recovering"
        assert client.get("/api/projects/recover-tool").status_code == 503
        assert client.get("/api/projects/recover-tool/submission-context").status_code == 503
        assert client.get("/api/projects/recover-tool/app/ping").status_code == 503
        preview = client.post(
            "/api/projects/recover-tool/preview-builds",
            headers={**_csrf(client, "recover-preview-key"), "Idempotency-Key": "recover-preview-key"},
        )
        assert preview.status_code == 503
        ready = client.get("/api/ready")
        assert ready.status_code == 503
        assert ready.json()["workspace_recovery"][0]["error_code"] == "workspace_recovery_failed"
        monkeypatch.setattr("app.asgi.os.replace", original_replace)

    with TestClient(create_app(config)) as recovered_client:
        recovered_client.get("/api/session")
        ready = _wait_ready(recovered_client)
        assert ready.status_code == 200, ready.text
        detail = recovered_client.get("/api/projects/recover-tool")
        assert detail.status_code == 200
        contents = {item["path"]: item["content"] for item in detail.json()["files"]}
        assert contents[frontend_file["path"]] == "frontend recovered"
        assert contents[backend_file["path"]] == "backend recovered"
        connection = connect(database)
        states = {row[0] for row in connection.execute("SELECT status FROM workspace_save_recovery").fetchall()}
        item_states = {row[0] for row in connection.execute("SELECT status FROM workspace_save_items").fetchall()}
        connection.close()
        assert states == {"completed"}
        assert item_states == {"promoted"}


def test_startup_recovers_verified_project_creation_halves(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    frontend.mkdir()
    backend.mkdir()
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend)
    monkeypatch.setattr(scaffold, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(scaffold, "BACKEND_PROJECTS_DIR", backend)
    database = tmp_path / "studio.sqlite3"
    migrate(database)
    operation_id = "project-create-" + "1" * 32
    connection = connect(database)
    with transaction(connection):
        connection.execute(
            "INSERT INTO project_create_reservations(slug,operation_id,actor_id,status,created_at,updated_at,display_name) "
            "VALUES ('crash-safe',?,'local-user','reserved',datetime('now'),datetime('now'),'Crash Safe')",
            (operation_id,),
        )
    connection.close()

    original_promote = scaffold.replace_directory_child

    def interrupt_backend_promotion(root, source_name, target_name):
        if Path(root) == backend and target_name == "crash-safe":
            raise OSError("simulated crash between promotions")
        return original_promote(root, source_name, target_name)

    monkeypatch.setattr(scaffold, "replace_directory_child", interrupt_backend_promotion)
    with pytest.raises(OSError, match="simulated crash"):
        scaffold.create_project(
            "Crash Safe", operation_id=operation_id,
            journal_root=database.parent / "project-create-journals",
            prepared_callback=lambda manifest: asgi_module._persist_project_creation_items(database, manifest),
        )
    monkeypatch.setattr(scaffold, "replace_directory_child", original_promote)

    assert (frontend / "crash-safe").is_dir()
    assert not (backend / "crash-safe").exists()
    with TestClient(create_app(StudioConfig(database_path=database))) as client:
        client.get("/api/session")
        ready = _wait_ready(client).json()
        assert ready == {"status": "ready"}
        assert client.get("/api/projects/crash-safe").status_code == 200
    assert (frontend / "crash-safe").is_dir() and (backend / "crash-safe").is_dir()
    connection = connect(database)
    assert connection.execute("SELECT status FROM project_create_reservations WHERE slug='crash-safe'").fetchone()[0] == "completed"
    assert {row[0] for row in connection.execute("SELECT status FROM project_create_items WHERE operation_id=?", (operation_id,))} == {"promoted"}
    connection.close()
