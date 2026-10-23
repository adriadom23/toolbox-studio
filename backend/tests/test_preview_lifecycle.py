from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.api.routers.preview import WorkspacePreviewBuildService
from app.api.schemas import PreviewGrantCreate
from app.asgi import create_app
from app.core.errors import PublicError
from app.core.studio_config import StudioConfig
from app.projects import registry
from app.submissions.build import BuildFailed, LocalSubprocessBuildProvider
from app.submissions.database import connect, transaction
from app.submissions.migrations import migrate
from app.submissions import preview as preview_module
from app.submissions.preview import PreviewManager
from app.submissions.providers import BuildOutputs
from app.submissions.repository import SubmissionRepository
from app.submissions.repository_v2 import PipelineRepository, now_iso
from app.submissions.runtime_refs import encode_runtime_ref, resolve_runtime_ref
from app.submissions.snapshot import SafeSnapshotBuilder, snapshot_manifest_json


def _sealed_snapshot(tmp_path: Path, runtime_root: Path):
    frontend = tmp_path / "workspace" / "frontend"
    backend = tmp_path / "workspace" / "backend"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("<template>demo</template>", encoding="utf-8")
    (backend / "demo" / "routes.py").write_text("sealed = True\n", encoding="utf-8")
    return SafeSnapshotBuilder(frontend, backend).build("demo", runtime_root / "sealed-input")


class _SuccessfulPreviewBuildProvider:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        preview = request.output_root / "preview"
        preview.mkdir(parents=True)
        (preview / "index.html").write_text("ready", encoding="utf-8")
        return BuildOutputs(True, "harness", ())


def _create_descendant_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("linked_tree", ["source", "runtime"])
def test_preview_seal_rejects_descendant_links_without_touching_external_files(tmp_path, linked_tree):
    database = tmp_path / "preview-links.sqlite3"
    migrate(database)
    manager = PreviewManager(database, tmp_path / "previews")
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text("safe", encoding="utf-8")
    runtime = tmp_path / "snapshot" / "backend"
    route = runtime / "app" / "projects" / "demo" / "routes.py"
    route.parent.mkdir(parents=True)
    route.write_text("sealed = True\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "secret.txt"
    canary.write_text("external-preview-link-canary", encoding="utf-8")
    before_mode = canary.stat().st_mode
    _create_descendant_directory_link((source if linked_tree == "source" else runtime) / "alias", outside)

    with pytest.raises(PublicError) as rejected:
        manager.seal(
            subject_kind="workspace",
            subject_id="demo",
            source=source,
            input_sha256="input",
            harness_sha256="harness",
            runtime_root=runtime,
            project_slug="demo",
        )

    expected_code = "preview_incompatible" if linked_tree == "source" else "preview_runtime_unavailable"
    assert rejected.value.code == expected_code
    assert canary.read_text(encoding="utf-8") == "external-preview-link-canary"
    assert canary.stat().st_mode == before_mode
    assert not any(
        path.is_file() and path.read_bytes() == b"external-preview-link-canary"
        for path in (tmp_path / "previews").rglob("*")
    )


def test_preview_api_rejects_noncanonical_public_ids_before_lookup(tmp_path):
    with TestClient(create_app(StudioConfig(database_path=tmp_path / "ids.sqlite3"))) as client:
        client.get("/api/session")
        headers = {
            "x-csrf-token": client.cookies.get("studio_csrf"),
            "Idempotency-Key": "preview-invalid-id",
        }
        assert client.get("/api/preview-operations/not-an-operation").status_code == 422
        assert client.post("/api/submissions/not-a-submission/preview-retries", headers=headers).status_code == 422
        invalid_preview = client.post(
            "/api/preview-grants",
            headers={"x-csrf-token": client.cookies.get("studio_csrf")},
            json={"subject_kind": "submission", "subject_id": "sub_" + "a" * 32, "preview_id": "prv_bad"},
        )
        assert invalid_preview.status_code == 422
        invalid_subject = client.post(
            "/api/preview-grants",
            headers={"x-csrf-token": client.cookies.get("studio_csrf")},
            json={"subject_kind": "submission", "subject_id": "sub_%2fescape"},
        )
        assert invalid_subject.status_code == 422
    schema = PreviewGrantCreate.model_json_schema()["properties"]
    assert schema["preview_id"]["anyOf"][0]["pattern"] == "^prv_[0-9a-f]{32}$"


def test_preview_requires_ready_and_grant_is_one_use_and_subject_bound(tmp_path):
    database = tmp_path / "preview.sqlite3"
    migrate(database)
    manager = PreviewManager(database, tmp_path / "objects")
    connection = connect(database)
    now = now_iso()
    connection.execute("INSERT INTO preview_objects(id,subject_kind,subject_id,input_sha256,harness_sha256,digest,state,created_at,updated_at) VALUES ('prv_00000000000000000000000000000000','submission','sub_11111111111111111111111111111111','i','h','d','building',?,?)", (now, now))
    connection.commit()
    connection.close()
    with pytest.raises(PublicError) as building:
        manager.create_grant("prv_00000000000000000000000000000000", subject_kind="submission", subject_id="sub_11111111111111111111111111111111", actor="alice")
    assert building.value.code == "preview_building"
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text("<script src='/app.js'></script>", encoding="utf-8")
    (source / "app.js").write_text("document.body.dataset.ready='yes'", encoding="utf-8")
    runtime = tmp_path / "snapshot" / "backend"
    route = runtime / "app" / "projects" / "demo" / "routes.py"
    route.parent.mkdir(parents=True)
    route.write_text("sealed = True\n", encoding="utf-8")
    preview = manager.seal(subject_kind="submission", subject_id="sub_22222222222222222222222222222222", source=source, input_sha256="input", harness_sha256="harness", runtime_root=runtime, project_slug="demo")
    grant = manager.create_grant(preview["id"], subject_kind="submission", subject_id="sub_22222222222222222222222222222222", actor="alice")
    with pytest.raises(PublicError):
        manager.exchange(grant["nonce"], preview_id=preview["id"], subject_kind="submission", subject_id="sub_wrong")
    token = manager.exchange(grant["nonce"], preview_id=preview["id"], subject_kind="submission", subject_id="sub_22222222222222222222222222222222")
    assert manager.authorize(token, preview["id"])["digest"] == preview["digest"]
    with pytest.raises(PublicError):
        manager.exchange(grant["nonce"], preview_id=preview["id"], subject_kind="submission", subject_id="sub_22222222222222222222222222222222")


def test_workspace_preview_selection_revokes_old_grants_and_sessions_without_deleting_objects(tmp_path):
    database = tmp_path / "preview-selection.sqlite3"
    migrate(database)
    root = tmp_path / "objects"
    manager = PreviewManager(database, root)

    def seal_version(label: str):
        source = tmp_path / f"build-{label}"
        source.mkdir()
        (source / "index.html").write_text(label, encoding="utf-8")
        runtime = tmp_path / f"snapshot-{label}" / "backend"
        route = runtime / "app" / "projects" / "demo" / "routes.py"
        route.parent.mkdir(parents=True)
        route.write_text(f"sealed = {label!r}\n", encoding="utf-8")
        return manager.seal(
            subject_kind="workspace",
            subject_id="demo",
            source=source,
            input_sha256=f"input-{label}",
            harness_sha256="harness",
            runtime_root=runtime,
            project_slug="demo",
        )

    first = seal_version("first")
    open_grant = manager.create_grant(first["id"], subject_kind="workspace", subject_id="demo", actor="alice")
    open_token = manager.exchange(
        open_grant["nonce"], preview_id=first["id"], subject_kind="workspace", subject_id="demo",
    )
    pending_grant = manager.create_grant(first["id"], subject_kind="workspace", subject_id="demo", actor="alice")
    first_object = root / first["root_ref"][:2] / first["root_ref"]

    second = seal_version("second")

    connection = connect(database)
    states = {
        row["id"]: (row["state"], row["error_code"])
        for row in connection.execute(
            "SELECT id,state,error_code FROM preview_objects WHERE subject_kind='workspace' AND subject_id='demo'"
        )
    }
    current = connection.execute(
        "SELECT id FROM preview_objects WHERE subject_kind='workspace' AND subject_id='demo' AND state='ready'"
    ).fetchall()
    connection.close()
    assert states[first["id"]] == ("unavailable", "preview_superseded")
    assert states[second["id"]] == ("ready", None)
    assert [row["id"] for row in current] == [second["id"]]
    assert first_object.is_dir()
    assert manager.authorize(open_token, first["id"]) is None
    assert manager.authorize_runtime(open_token, "demo") is None
    with pytest.raises(PublicError) as pending_rejected:
        manager.exchange(
            pending_grant["nonce"], preview_id=first["id"], subject_kind="workspace", subject_id="demo",
        )
    assert pending_rejected.value.code == "invalid_preview_grant"
    with pytest.raises(PublicError) as stale_grant:
        manager.create_grant(first["id"], subject_kind="workspace", subject_id="demo", actor="alice")
    assert stale_grant.value.code == "preview_unavailable"
    assert manager.create_grant(second["id"], subject_kind="workspace", subject_id="demo", actor="alice")["preview_id"] == second["id"]


def test_preview_status_reports_current_workspace_selection(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend" / "src" / "projects"
    backend = tmp_path / "backend" / "app" / "projects"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("<template>demo</template>", encoding="utf-8")
    (backend / "demo" / "routes.py").write_text("routes = True\n", encoding="utf-8")
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend)
    database = tmp_path / "status.sqlite3"
    config = StudioConfig(database_path=database, storage_min_free_bytes=1)
    with TestClient(create_app(config)) as client:
        client.get("/api/session")
        manager = PreviewManager(database, tmp_path / "previews")

        def seal_version(label: str):
            source = tmp_path / f"status-build-{label}"
            source.mkdir()
            (source / "index.html").write_text(label, encoding="utf-8")
            runtime = tmp_path / f"status-runtime-{label}" / "backend"
            route = runtime / "app" / "projects" / "demo" / "routes.py"
            route.parent.mkdir(parents=True)
            route.write_text(f"sealed = {label!r}\n", encoding="utf-8")
            return manager.seal(
                subject_kind="workspace", subject_id="demo", source=source,
                input_sha256=f"input-{label}", harness_sha256="harness",
                runtime_root=runtime, project_slug="demo",
            )

        first = seal_version("first")
        second = seal_version("second")
        stale = client.get(f"/api/preview-status/{first['id']}")
        current = client.get(f"/api/preview-status/{second['id']}")

    assert stale.status_code == 200
    assert stale.json() == {
        "preview_id": first["id"], "state": "unavailable", "error_code": "preview_superseded",
        "current": False, "current_preview_id": second["id"],
    }
    assert current.status_code == 200
    assert current.json()["current"] is True


@pytest.mark.parametrize("iteration", range(8))
def test_concurrent_preview_seals_quarantine_losing_staging(tmp_path, monkeypatch, iteration):
    database = tmp_path / "preview-race.sqlite3"
    migrate(database)
    manager = PreviewManager(database, tmp_path / "objects")
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text(f"ready-{iteration}", encoding="utf-8")
    barrier = __import__("threading").Barrier(2)
    original_copy = preview_module._copy_regular_files

    def synchronized_copy(source_root, destination, expected):
        original_copy(source_root, destination, expected)
        barrier.wait(timeout=2)

    monkeypatch.setattr(preview_module, "_copy_regular_files", synchronized_copy)
    kwargs = {
        "subject_kind": "workspace",
        "subject_id": "demo",
        "source": source,
        "input_sha256": f"input-{iteration}",
        "harness_sha256": "harness",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(manager.seal, **kwargs), pool.submit(manager.seal, **kwargs))]

    assert results[0]["id"] == results[1]["id"]
    assert not list((tmp_path / "objects").rglob("*.staging-*"))
    quarantines = list((tmp_path / "objects").rglob("*.quarantine-*"))
    assert len(quarantines) == 1
    connection = connect(database)
    reservation = connection.execute(
        "SELECT kind,owner_id,bytes,state FROM storage_reservations WHERE kind='preview-race'"
    ).fetchone()
    connection.close()
    assert reservation["state"] == "quarantined"
    assert reservation["owner_id"] == quarantines[0].name
    assert reservation["bytes"] == len(f"ready-{iteration}")


def test_workspace_preview_operation_replays_recovers_and_records_failure(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend" / "src" / "projects"
    backend = tmp_path / "backend" / "app" / "projects"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("<template>demo</template>", encoding="utf-8")
    (backend / "demo" / "routes.py").write_text("routes = True\n", encoding="utf-8")
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend)
    database = tmp_path / "operations.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()
    config = StudioConfig(
        database_path=database,
        snapshot_max_bytes=1024 * 1024,
        artifact_max_bytes=1024 * 1024,
        storage_min_free_bytes=1,
        storage_spool_max_bytes=10 * 1024 * 1024,
    )
    service = WorkspacePreviewBuildService(database, tmp_path / "runtime", config)
    for invalid_key in ("", "short", "x" * 129, "invalid key", "inválida", "line\nbreak"):
        with pytest.raises(PublicError) as invalid:
            service.reserve("demo", "alice", invalid_key)
        assert invalid.value.code == "invalid_idempotency_key" and invalid.value.status_code == 422
    builds = []

    def successful_build(_provider, request):
        builds.append(request.output_root)
        preview = request.output_root / "preview"
        preview.mkdir(parents=True)
        (preview / "index.html").write_text("ready", encoding="utf-8")
        return BuildOutputs(True, "harness", ())

    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", successful_build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in (
            pool.submit(service.reserve, "demo", "alice", "preview-key"),
            pool.submit(service.reserve, "demo", "alice", "preview-other-key"),
        )]
    assert sum(int(created) for _response, created in (first, second)) == 1
    assert first[0]["operation_id"] == second[0]["operation_id"]
    reserved = first[0]
    winning_key = "preview-key" if first[1] else "preview-other-key"
    replay, replay_created = service.reserve("demo", "alice", winning_key)
    assert not replay_created and replay["operation_id"] == reserved["operation_id"]
    service.run(reserved["operation_id"], recover=True)
    ready = service._response(service.get(reserved["operation_id"]))
    assert ready["state"] == "ready" and ready["preview_id"]
    service.run(reserved["operation_id"], recover=True)
    assert len(builds) == 1

    failed, _ = service.reserve("demo", "alice", "failed-key")
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda _provider, _request: (_ for _ in ()).throw(BuildFailed("build_timeout")))
    service.run(failed["operation_id"], recover=True)
    failure = service._response(service.get(failed["operation_id"]))
    assert failure["state"] == "failed"
    assert failure["error_code"] == "build_timeout"
    assert failure["recoverable"] is True

    connection = connect(database)
    with transaction(connection):
        now = now_iso()
        connection.execute(
            "INSERT INTO storage_reservations(id,kind,owner_id,bytes,state,expires_at,updated_at) "
            "VALUES ('preview-project-capacity','preview','active-other',1,'held',?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO preview_build_operations(operation_id,actor_id,project_slug,idempotency_key,request_sha256,state,phase,"
            "reservation_id,created_at,updated_at,subject_kind,subject_id,input_sha256,harness_sha256) "
            "VALUES ('active-other','reviewer','demo','active-other-key','request','queued','reserved',?,?,?,?,?,?,?)",
            ("preview-project-capacity", now, now, "submission", "sub_" + "c" * 32, "input", "harness"),
        )
    connection.close()
    with pytest.raises(PublicError) as capacity:
        service.reserve("demo", "alice", "capacity-key")
    assert capacity.value.code == "project_build_capacity_exceeded"


def test_preview_recovery_rejects_mutated_snapshot_and_output(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend" / "src" / "projects"
    backend = tmp_path / "backend" / "app" / "projects"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("<template>demo</template>", encoding="utf-8")
    (backend / "demo" / "routes.py").write_text("sealed = True\n", encoding="utf-8")
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend)
    database = tmp_path / "preview-recovery.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()
    runtime_root = tmp_path / "runtime"
    provider = _SuccessfulPreviewBuildProvider()
    service = WorkspacePreviewBuildService(
        database,
        runtime_root,
        StudioConfig(
            database_path=database,
            snapshot_max_bytes=1024 * 1024,
            artifact_max_bytes=1024 * 1024,
            storage_min_free_bytes=1,
            storage_spool_max_bytes=10 * 1024 * 1024,
        ),
        build_provider=provider,
    )

    def expire(operation_id: str) -> None:
        connection = connect(database)
        connection.execute(
            "UPDATE preview_build_operations SET lease_until=? WHERE operation_id=?",
            ("2000-01-01T00:00:00+00:00", operation_id),
        )
        connection.commit()
        connection.close()

    snapshot_operation, _ = service.reserve("demo", "alice", "snapshot-crash-key")
    original_update = service._update

    def crash_after_snapshot(operation_id, worker, generation, **values):
        original_update(operation_id, worker, generation, **values)
        if "snapshot_manifest_json" in values:
            raise SystemExit("simulated crash after snapshot persistence")

    service._update = crash_after_snapshot
    with pytest.raises(SystemExit, match="snapshot persistence"):
        service.run(str(snapshot_operation["operation_id"]), recover=True)
    service._update = original_update
    snapshot_row = service.get(str(snapshot_operation["operation_id"]))
    snapshot_root = resolve_runtime_ref(runtime_root, str(snapshot_row["snapshot_ref"]))
    (snapshot_root / "frontend" / "src" / "projects" / "demo" / "Demo.vue").write_text(
        "<template>changed</template>", encoding="utf-8",
    )
    expire(str(snapshot_operation["operation_id"]))
    service.run(str(snapshot_operation["operation_id"]), recover=True)
    snapshot_failed = service.get(str(snapshot_operation["operation_id"]))
    assert snapshot_failed["state"] == "failed"
    assert snapshot_failed["error_code"] == "preview_integrity_failed"
    assert provider.calls == 0

    output_operation, _ = service.reserve("demo", "alice", "output-crash-key")

    def crash_after_output(operation_id, worker, generation, **values):
        original_update(operation_id, worker, generation, **values)
        if "output_manifest_json" in values:
            raise SystemExit("simulated crash after output persistence")

    service._update = crash_after_output
    with pytest.raises(SystemExit, match="output persistence"):
        service.run(str(output_operation["operation_id"]), recover=True)
    service._update = original_update
    output_row = service.get(str(output_operation["operation_id"]))
    output_root = resolve_runtime_ref(runtime_root, str(output_row["output_ref"]))
    (output_root / "preview" / "index.html").write_text("changed", encoding="utf-8")
    expire(str(output_operation["operation_id"]))
    service.run(str(output_operation["operation_id"]), recover=True)
    output_failed = service.get(str(output_operation["operation_id"]))
    assert output_failed["state"] == "failed"
    assert output_failed["error_code"] == "preview_integrity_failed"
    assert provider.calls == 1


def test_preview_recovery_does_not_adopt_an_active_lease(tmp_path):
    database = tmp_path / "preview-lease.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        lease_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        connection.execute(
            "INSERT INTO storage_reservations(id,kind,owner_id,bytes,state,expires_at,updated_at) "
            "VALUES ('lease-reservation','preview','pop_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,'held',?,?)",
            (lease_until, now),
        )
        connection.execute(
            "INSERT INTO preview_build_operations(operation_id,actor_id,project_slug,idempotency_key,request_sha256,state,phase,"
            "reservation_id,lease_owner,lease_generation,lease_until,created_at,updated_at,subject_kind,subject_id) "
            "VALUES ('pop_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','alice','demo','active-lease-key','request','running','build',"
            "'lease-reservation','worker-a',7,?,?,?,?,?)",
            (lease_until, now, now, "workspace", "demo"),
        )
    connection.close()
    config = StudioConfig(database_path=database, storage_min_free_bytes=1)
    first = WorkspacePreviewBuildService(database, tmp_path / "runtime", config)
    second = WorkspacePreviewBuildService(database, tmp_path / "runtime", config)
    assert second.recover() == []
    row, generation = second._claim("pop_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "worker-b", True)
    assert row is None and generation is None
    connection = connect(database)
    current = connection.execute(
        "SELECT lease_owner,lease_generation FROM preview_build_operations WHERE operation_id=?",
        ("pop_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
    ).fetchone()
    connection.close()
    assert current["lease_owner"] == "worker-a" and current["lease_generation"] == 7
    assert first.get("pop_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")["state"] == "running"


def test_submission_preview_retry_is_authorized_idempotent_concurrent_and_audited(tmp_path):
    database = tmp_path / "submission-retry.sqlite3"
    migrate(database)
    submission_id = "sub_" + "a" * 32
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    snapshot = _sealed_snapshot(tmp_path, runtime_root)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(
            submission_id=submission_id, slug="demo", version=1, actor="alice",
            title="T", description="D", correlation_id="submission-correlation",
        )
        connection.execute(
            "UPDATE submission_records SET input_sha256=?,harness_sha256='harness',snapshot_ref=?,snapshot_manifest_json=? WHERE id=?",
            (
                snapshot.input_sha256,
                encode_runtime_ref(runtime_root, snapshot.root),
                snapshot_manifest_json(snapshot),
                submission_id,
            ),
        )
    connection.close()
    manager = PreviewManager(database, tmp_path / "runtime" / "previews")
    manager.unavailable(
        subject_kind="submission", subject_id=submission_id, input_sha256=snapshot.input_sha256,
        harness_sha256="harness", code="build_timeout",
    )
    build_provider = _SuccessfulPreviewBuildProvider()
    service = WorkspacePreviewBuildService(
        database,
        tmp_path / "runtime",
        StudioConfig(database_path=database, storage_min_free_bytes=1, storage_spool_max_bytes=1024 * 1024 * 1024),
        build_provider=build_provider,
    )

    with pytest.raises(PublicError) as forbidden:
        service.reserve_submission(submission_id, "alice", {"submitter"}, "retry-forbidden", "request-correlation")
    assert forbidden.value.code == "forbidden"

    def reserve_once(key):
        return service.reserve_submission(submission_id, "reviewer", {"reviewer"}, key, "request-correlation")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in (
            pool.submit(reserve_once, "retry-concurrent-a"),
            pool.submit(reserve_once, "retry-concurrent-b"),
        )]
    responses = [first, second]
    assert sum(int(created) for _response, created in responses) == 1
    assert responses[0][0]["operation_id"] == responses[1][0]["operation_id"]
    assert responses[0][0]["timeout_seconds"] == service.config.build_timeout_seconds
    operation_id = str(responses[0][0]["operation_id"])
    winning_key = "retry-concurrent-a" if first[1] else "retry-concurrent-b"
    service.run(operation_id, recover=True)
    ready = service._response(service.get(operation_id))
    assert ready["state"] == "ready" and ready["preview_id"]
    replay, created = reserve_once(winning_key)
    assert created is False and replay["operation_id"] == operation_id

    connection = connect(database)
    events = connection.execute(
        "SELECT event_type,actor_id,correlation_id,data_json FROM submission_operation_events WHERE submission_id=? AND event_type LIKE 'preview_retry_%' ORDER BY seq",
        (submission_id,),
    ).fetchall()
    connection.close()
    assert [event["event_type"] for event in events] == ["preview_retry_reserved", "preview_retry_succeeded"]
    assert all(event["actor_id"] == "reviewer" and event["correlation_id"] == "request-correlation" for event in events)
    assert "recoverable_preview_error:build_timeout" in events[0]["data_json"]


def test_submission_preview_retry_records_recoverable_failure(tmp_path, monkeypatch):
    database = tmp_path / "submission-retry-failure.sqlite3"
    migrate(database)
    submission_id = "sub_" + "b" * 32
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    snapshot = _sealed_snapshot(tmp_path, runtime_root)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(
            submission_id=submission_id, slug="demo", version=1, actor="alice",
            title="T", description="D", correlation_id=None,
        )
        connection.execute(
            "UPDATE submission_records SET input_sha256=?,harness_sha256='harness',snapshot_ref=?,snapshot_manifest_json=? WHERE id=?",
            (
                snapshot.input_sha256,
                encode_runtime_ref(runtime_root, snapshot.root),
                snapshot_manifest_json(snapshot),
                submission_id,
            ),
        )
    connection.close()
    PreviewManager(database, tmp_path / "runtime" / "previews").unavailable(
        subject_kind="submission", subject_id=submission_id, input_sha256=snapshot.input_sha256,
        harness_sha256="harness", code="build_timeout",
    )
    service = WorkspacePreviewBuildService(
        database,
        tmp_path / "runtime",
        StudioConfig(database_path=database, storage_min_free_bytes=1, storage_spool_max_bytes=1024 * 1024 * 1024),
    )
    monkeypatch.setattr(
        LocalSubprocessBuildProvider,
        "execute",
        lambda _provider, _request: (_ for _ in ()).throw(BuildFailed("build_timeout")),
    )
    reserved, created = service.reserve_submission(
        submission_id, "reviewer", {"reviewer"}, "retry-failure", "failure-correlation",
    )
    assert created
    service.run(str(reserved["operation_id"]), recover=True)
    failed = service._response(service.get(str(reserved["operation_id"])))
    assert failed["state"] == "failed" and failed["error_code"] == "build_timeout" and failed["recoverable"] is True
