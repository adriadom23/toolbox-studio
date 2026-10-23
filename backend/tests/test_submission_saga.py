from __future__ import annotations

import hashlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.api.schemas import ProjectFilesUpdate
from app.asgi import _save_project_files
from app.projects import registry
from app.submissions.build import LocalSubprocessBuildProvider
from app.submissions.database import connect, transaction
from app.submissions.logs import LogExportDispatcher
from app.submissions.migrations import migrate
from app.submissions.jobs import LeaseLost, claim_job, heartbeat_job, require_job_lease
from app.submissions.provider_factory import create_local_providers
from app.submissions.providers import BuildHandle, BuildOutputs, BuildPoll, BuildRequest, GitMetadata, ProviderResult
from app.submissions.recovery import recover_interrupted_submissions
from app.submissions.repository import SubmissionRepository
from app.submissions.saga import SubmissionSaga
from app.submissions.snapshot import SafeSnapshotBuilder


def test_saga_commits_only_after_verified_artifact(tmp_path, monkeypatch):
    database = tmp_path / "db.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    connection.close()
    monkeypatch.setattr(LocalSubprocessBuildProvider, "execute", lambda self, request: BuildOutputs(True, "harness", ()))
    saga = SubmissionSaga(database, tmp_path / "runtime", create_local_providers(tmp_path / "runtime"), frontend_root=registry.FRONTEND_PROJECTS_DIR.parents[1])
    reserved, created = saga.reserve(slug="demo-ping", actor="alice", title="Demo", description="Description", idempotency_key="abcdefgh")
    assert created
    result = saga.run(reserved["operation_id"], reserved["submission_id"])
    assert result["status"] == "submitted"
    assert next(phase for phase in result["phases"] if phase["phase"] == "notify")["status"] == "succeeded"
    assert result["notifications"] == [{"provider_key": "local", "status": "delivered", "error_code": None}]
    replay, replay_created = saga.reserve(
        slug="demo-ping", actor="alice", title="Demo", description="Description", idempotency_key="abcdefgh",
    )
    assert not replay_created
    assert replay["notifications"] == result["notifications"]
    assert next(phase for phase in replay["phases"] if phase["phase"] == "notify")["status"] == "succeeded"
    connection = connect(database)
    row = connection.execute("SELECT state,artifact_ref,artifact_sha256,artifact_size FROM submission_records").fetchone()
    assert row["state"] == "submitted" and all(row[key] for key in ("artifact_ref", "artifact_sha256", "artifact_size"))
    effect = connection.execute("SELECT kind,status FROM outbox_effects WHERE submission_id=?", (reserved["submission_id"],)).fetchone()
    assert dict(effect) == {"kind": "notification", "status": "delivered"}
    connection.execute("UPDATE submission_records SET state='changes_requested' WHERE id=?", (reserved["submission_id"],))
    connection.close()

    successor, created = saga.reserve(
        slug="demo-ping", actor="alice", title="Revision", description="Updated",
        predecessor_id=reserved["submission_id"], idempotency_key="successor-key",
    )
    assert created
    connection = connect(database)
    assert connection.execute("SELECT predecessor_id FROM submission_records WHERE id=?", (successor["submission_id"],)).fetchone()[0] == reserved["submission_id"]
    connection.close()


@pytest.mark.parametrize(
    ("provider_result", "expected_phase", "expected_effect"),
    ((False, "failed", "failed"), (None, "delivery_unknown", "delivery_unknown")),
)
def test_notification_terminal_failure_is_reflected_in_operation_and_replay(
    tmp_path, monkeypatch, provider_result, expected_phase, expected_effect,
):
    database = tmp_path / f"notify-{expected_phase}.sqlite3"
    runtime = tmp_path / f"runtime-{expected_phase}"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    connection.close()
    providers = create_local_providers(runtime)
    monkeypatch.setattr(providers.build, "execute", lambda request: BuildOutputs(True, "harness", ()))

    class TerminalNotifier:
        def send(self, *_args):
            if provider_result is None:
                raise TimeoutError("ambiguous delivery")
            return ProviderResult(provider_result)

    providers.notifications["local"] = TerminalNotifier()
    saga = SubmissionSaga(database, runtime, providers, frontend_root=registry.FRONTEND_PROJECTS_DIR.parents[1])
    reserved, _ = saga.reserve(
        slug="demo-ping", actor="alice", title="Demo", description="Description",
        idempotency_key=f"notify-{expected_phase}-key",
    )
    result = saga.run(reserved["operation_id"], reserved["submission_id"])
    assert result["status"] == "submitted"
    assert next(phase for phase in result["phases"] if phase["phase"] == "notify")["status"] == expected_phase
    assert result["notifications"][0]["status"] == expected_effect

    replay, created = saga.reserve(
        slug="demo-ping", actor="alice", title="Demo", description="Description",
        idempotency_key=f"notify-{expected_phase}-key",
    )
    assert not created
    assert replay["notifications"][0]["status"] == expected_effect
    assert next(phase for phase in replay["phases"] if phase["phase"] == "notify")["status"] == expected_phase


def test_remote_log_export_timeout_never_overlaps_and_late_success_reconciles(tmp_path, monkeypatch):
    database = tmp_path / "log-export.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()

    release = threading.Event()

    class BlockingExporter:
        def __init__(self):
            self.calls = 0

        def append(self, _ref, _phase, _content):
            self.calls += 1
            release.wait(2)

    exporter = BlockingExporter()
    local = create_local_providers(tmp_path / "runtime")
    providers = SimpleNamespace(**{**local.__dict__, "log_exporter": exporter})
    monkeypatch.setattr(
        providers.build,
        "execute",
        lambda _request: BuildOutputs(True, "harness", ({"phase": "frontend", "stdout": "safe log\n", "stderr": ""},)),
    )
    saga = SubmissionSaga(database, tmp_path / "runtime", providers, frontend_root=registry.FRONTEND_PROJECTS_DIR.parents[1])
    reserved, _ = saga.reserve(
        slug="demo-ping", actor="alice", title="Demo", description="Description", idempotency_key="log-export-key",
    )
    result = saga.run(reserved["operation_id"], reserved["submission_id"])
    assert result["status"] == "submitted"

    dispatcher = LogExportDispatcher(database, providers.logs, providers.log_exporter, timeout_seconds=0.05)
    assert dispatcher.dispatch_pending(limit=1) == 0
    assert exporter.calls == 1
    mutation = connect(database)
    with transaction(mutation):
        mutation.execute("UPDATE projects SET display_name=display_name WHERE slug='demo-ping'")
    mutation.close()
    for _ in range(3):
        assert LogExportDispatcher(database, providers.logs, providers.log_exporter, timeout_seconds=0.01).dispatch_pending(limit=1) == 0
    assert exporter.calls == 1
    connection = connect(database)
    ambiguous = connection.execute(
        "SELECT status,attempt,last_error_code FROM submission_log_exports WHERE submission_id=? AND phase='frontend'",
        (reserved["submission_id"],),
    ).fetchone()
    connection.close()
    assert dict(ambiguous) == {"status": "exporting", "attempt": 1, "last_error_code": "log_export_timeout"}

    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        connection = connect(database)
        export = connection.execute(
            "SELECT status,attempt,last_error_code FROM submission_log_exports WHERE submission_id=? AND phase='frontend'",
            (reserved["submission_id"],),
        ).fetchone()
        connection.close()
        if export["status"] == "exported":
            break
        time.sleep(0.01)
    assert dict(export) == {"status": "exported", "attempt": 1, "last_error_code": None}
    assert exporter.calls == 1


def test_recovery_reuses_original_sealed_snapshot_after_workspace_changes(tmp_path, monkeypatch):
    frontend_projects = tmp_path / "frontend" / "src" / "projects"
    backend_projects = tmp_path / "backend" / "app" / "projects"
    frontend_file = frontend_projects / "demo" / "Demo.vue"
    backend_file = backend_projects / "demo" / "routes.py"
    frontend_file.parent.mkdir(parents=True)
    backend_file.parent.mkdir(parents=True)
    frontend_file.write_text("original frontend", encoding="utf-8")
    backend_file.write_text("original backend", encoding="utf-8")
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend_projects)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend_projects)

    database = tmp_path / "immutable-recovery.sqlite3"
    runtime = tmp_path / "runtime"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()
    providers = create_local_providers(runtime, frontend_root=tmp_path / "frontend", backend_root=tmp_path / "backend")
    monkeypatch.setattr(providers.build, "execute", lambda request: BuildOutputs(True, "harness", ()))
    saga = SubmissionSaga(database, runtime, providers, frontend_root=tmp_path / "frontend")
    reserved, _ = saga.reserve(
        slug="demo", actor="alice", title="Demo", description="Description", idempotency_key="immutable-recovery-key",
    )
    original_execute = saga._execute_build

    def crash_after_snapshot(*_args):
        raise LeaseLost("simulated post-snapshot crash")

    monkeypatch.setattr(saga, "_execute_build", crash_after_snapshot)
    assert saga.run(reserved["operation_id"], reserved["submission_id"])["status"] == "building"
    connection = connect(database)
    before = connection.execute(
        "SELECT snapshot_ref,input_sha256,snapshot_manifest_json FROM submission_records WHERE id=?",
        (reserved["submission_id"],),
    ).fetchone()
    assert before["snapshot_ref"] and before["snapshot_manifest_json"]
    connection.execute(
        "UPDATE build_jobs SET lease_until=? WHERE submission_id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), reserved["submission_id"]),
    )
    connection.commit()
    connection.close()

    frontend_file.write_text("later frontend edit", encoding="utf-8")
    monkeypatch.setattr(saga, "_execute_build", original_execute)
    assert recover_interrupted_submissions(
        database,
        providers.artifacts,
        resume_callback=saga.run,
    ) == [reserved["submission_id"]]

    connection = connect(database)
    after = connection.execute(
        "SELECT state,snapshot_ref,input_sha256,manifest_json FROM submission_records WHERE id=?",
        (reserved["submission_id"],),
    ).fetchone()
    connection.close()
    manifest = __import__("json").loads(after["manifest_json"])
    frontend_entry = next(item for item in manifest["files"] if item["path"] == "frontend/demo/Demo.vue")
    assert after["state"] == "submitted"
    assert after["snapshot_ref"] == before["snapshot_ref"]
    assert after["input_sha256"] == before["input_sha256"]
    assert frontend_entry["sha256"] == hashlib.sha256(b"original frontend").hexdigest()
    assert frontend_entry["sha256"] != hashlib.sha256(b"later frontend edit").hexdigest()
    assert len(list((runtime / "inputs").iterdir())) == 1


def test_submission_snapshot_and_concurrent_save_are_serialized_per_project(tmp_path, monkeypatch):
    frontend_projects = tmp_path / "frontend" / "src" / "projects"
    backend_projects = tmp_path / "backend" / "app" / "projects"
    frontend_file = frontend_projects / "demo" / "Demo.vue"
    backend_file = backend_projects / "demo" / "routes.py"
    frontend_file.parent.mkdir(parents=True)
    backend_file.parent.mkdir(parents=True)
    frontend_file.write_text("old frontend", encoding="utf-8")
    backend_file.write_text("old backend", encoding="utf-8")
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend_projects)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend_projects)

    database = tmp_path / "snapshot-lock.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()
    providers = create_local_providers(tmp_path / "runtime")
    monkeypatch.setattr(providers.build, "execute", lambda request: BuildOutputs(True, "harness", ()))
    saga = SubmissionSaga(database, tmp_path / "runtime", providers, frontend_root=tmp_path / "frontend")
    reserved, _ = saga.reserve(slug="demo", actor="alice", title="Demo", description="Description", idempotency_key="snapshot-lock-key")

    entered = threading.Event()
    release = threading.Event()
    original_build = SafeSnapshotBuilder.build

    def pause_snapshot(builder, slug, destination):
        entered.set()
        assert release.wait(2)
        return original_build(builder, slug, destination)

    monkeypatch.setattr(SafeSnapshotBuilder, "build", pause_snapshot)
    update = ProjectFilesUpdate(files=[{
        "path": "frontend/Demo.vue",
        "content": "new frontend",
        "base_sha256": hashlib.sha256(b"old frontend").hexdigest(),
    }])
    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(saga.run, reserved["operation_id"], reserved["submission_id"])
        assert entered.wait(2)
        save = pool.submit(_save_project_files, database, "demo", update)
        time.sleep(0.05)
        assert not save.done()
        release.set()
        assert submission.result(timeout=10)["status"] == "submitted"
        save.result(timeout=10)
    sealed = tmp_path / "runtime" / "inputs" / f"{reserved['submission_id']}-g1" / "frontend" / "src" / "projects" / "demo" / "Demo.vue"
    assert sealed.read_text(encoding="utf-8") == "old frontend"
    assert frontend_file.read_text(encoding="utf-8") == "new frontend"


def test_concurrent_saves_recover_only_their_own_project_journal(tmp_path, monkeypatch):
    frontend_projects = tmp_path / "frontend" / "src" / "projects"
    backend_projects = tmp_path / "backend" / "app" / "projects"
    for slug in ("project-a", "project-b"):
        frontend_file = frontend_projects / slug / "Tool.vue"
        backend_file = backend_projects / slug / "routes.py"
        frontend_file.parent.mkdir(parents=True)
        backend_file.parent.mkdir(parents=True)
        frontend_file.write_text(f"old {slug}", encoding="utf-8")
        backend_file.write_text("routes = True", encoding="utf-8")
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", frontend_projects)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", backend_projects)
    database = tmp_path / "save-locks.sqlite3"
    migrate(database)
    connection = connect(database)
    for slug in ("project-a", "project-b"):
        SubmissionRepository(connection).upsert_project(slug=slug, display_name=slug, created_at=None, modified_at=None)
    connection.commit()
    connection.close()

    entered_a = threading.Event()
    release_a = threading.Event()
    original_replace = __import__("os").replace

    def pause_project_a(source, target):
        if Path(target) == frontend_projects / "project-a" / "Tool.vue":
            entered_a.set()
            assert release_a.wait(5)
        return original_replace(source, target)

    monkeypatch.setattr("app.asgi.os.replace", pause_project_a)
    updates = {
        slug: ProjectFilesUpdate(files=[{
            "path": "frontend/Tool.vue",
            "content": f"new {slug}",
            "base_sha256": hashlib.sha256(f"old {slug}".encode()).hexdigest(),
        }])
        for slug in ("project-a", "project-b")
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_save_project_files, database, "project-a", updates["project-a"])
        assert entered_a.wait(2)
        second = pool.submit(_save_project_files, database, "project-b", updates["project-b"])
        second.result(timeout=2)
        release_a.set()
        first.result(timeout=5)
    assert (frontend_projects / "project-a" / "Tool.vue").read_text(encoding="utf-8") == "new project-a"
    assert (frontend_projects / "project-b" / "Tool.vue").read_text(encoding="utf-8") == "new project-b"
    connection = connect(database)
    assert connection.execute("SELECT COUNT(*) FROM workspace_save_recovery WHERE status='completed'").fetchone()[0] == 2
    connection.close()


def test_git_clean_policy_blocks_before_build(tmp_path, monkeypatch):
    database = tmp_path / "git-clean.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()
    providers = create_local_providers(tmp_path / "runtime")
    monkeypatch.setattr(providers.git, "inspect", lambda root: GitMetadata("abcdef1", "main", None, True, ()))
    build_called = False

    def build(_request):
        nonlocal build_called
        build_called = True
        return BuildOutputs(True, "harness", ())

    monkeypatch.setattr(providers.build, "execute", build)
    saga = SubmissionSaga(
        database, tmp_path / "runtime", providers,
        frontend_root=registry.FRONTEND_PROJECTS_DIR.parents[1], git_require_clean=True,
    )
    reserved, _ = saga.reserve(
        slug="demo-ping", actor="alice", title="Demo", description="Description",
        idempotency_key="git-clean-key",
    )
    result = saga.run(reserved["operation_id"], reserved["submission_id"])
    assert result["status"] == "build_failed"
    assert result["error"] == "git_not_clean"
    assert build_called is False


def test_build_lease_heartbeat_and_generation_fence(tmp_path):
    database = tmp_path / "lease.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    saga = SubmissionSaga(database, tmp_path / "runtime", create_local_providers(tmp_path / "runtime"), frontend_root=registry.FRONTEND_PROJECTS_DIR.parents[1])
    reserved, _ = saga.reserve(slug="demo-ping", actor="alice", title="Demo", description="Description", idempotency_key="lease-test-key")
    connection.close()

    connection = connect(database)
    generation = claim_job(connection, reserved["submission_id"], "worker-a", lease_seconds=1)
    assert generation == 1
    assert heartbeat_job(connection, reserved["submission_id"], "worker-a", generation, lease_seconds=60)
    require_job_lease(connection, reserved["submission_id"], "worker-a", generation)
    connection.execute("UPDATE build_jobs SET lease_until=? WHERE submission_id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), reserved["submission_id"]))
    next_generation = claim_job(connection, reserved["submission_id"], "worker-b", lease_seconds=60)
    assert next_generation == 2
    with pytest.raises(LeaseLost):
        require_job_lease(connection, reserved["submission_id"], "worker-a", generation)
    connection.close()


def test_long_build_renews_lease_and_concurrent_recovery_does_not_adopt(tmp_path, monkeypatch):
    database = tmp_path / "long-build.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    connection.close()
    providers = create_local_providers(tmp_path / "runtime")

    def delayed_build(_request):
        time.sleep(1.6)
        return BuildOutputs(True, "harness", ())

    monkeypatch.setattr(providers.build, "execute", delayed_build)
    saga = SubmissionSaga(database, tmp_path / "runtime", providers, frontend_root=registry.FRONTEND_PROJECTS_DIR.parents[1], lease_seconds=1, heartbeat_interval=0.2)
    reserved, _ = saga.reserve(slug="demo-ping", actor="alice", title="Demo", description="Description", idempotency_key="long-build-key")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(saga.run, reserved["operation_id"], reserved["submission_id"])
        time.sleep(1.2)
        assert recover_interrupted_submissions(database, providers.artifacts) == []
        result = future.result(timeout=10)
    assert result["status"] == "submitted"


def test_remote_recovery_adopts_persisted_build_identity_without_starting_again(tmp_path):
    database = tmp_path / "remote-recovery.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()

    class RemoteBuild:
        provider = "codebuild"
        harness_sha256 = "harness"

        def __init__(self):
            self.starts = 0
            self.reconciles = 0

        def prepare_input(self, request):
            return {"input_version_id": "version-1", "input_archive_sha256": "a" * 64}

        def start(self, request, expected=None):
            self.starts += 1
            assert expected
            return BuildHandle("new-build", request.marker)

        def reconcile(self, marker, window_seconds):
            self.reconciles += 1
            return [BuildHandle("accepted-build", marker)]

        def poll(self, handle):
            assert handle.id == "accepted-build"
            return BuildPoll("succeeded")

        def fetch_outputs(self, handle, expected):
            assert expected == {
                "input_sha256": "input",
                "harness_sha256": "harness",
                "input_version_id": "version-1",
                "input_archive_sha256": "a" * 64,
            }
            return BuildOutputs(True, "harness", ())

    remote = RemoteBuild()
    providers = SimpleNamespace(build=remote, review_base_url="http://127.0.0.1:5173")
    saga = SubmissionSaga(database, tmp_path / "runtime", providers, heartbeat_interval=0.01)
    reserved, _ = saga.reserve(slug="demo-ping", actor="alice", title="Demo", description="Description", idempotency_key="remote-recovery-key")
    connection = connect(database)
    generation = claim_job(connection, reserved["submission_id"], "recovery-worker", lease_seconds=60)
    connection.execute(
        "UPDATE build_jobs SET start_marker='persisted-marker',external_id='accepted-build',"
        "input_version_id='version-1',input_archive_sha256=? WHERE submission_id=?",
        ("a" * 64, reserved["submission_id"]),
    )
    connection.commit()
    connection.close()
    result = saga._execute_remote_with_heartbeat(
        BuildRequest("demo-ping", tmp_path, tmp_path / "out", 5, "input", "new-marker"),
        reserved["submission_id"], "recovery-worker", generation,
    )
    assert result.ok
    assert remote.reconciles == 0
    assert remote.starts == 0
    connection = connect(database)
    row = connection.execute(
        "SELECT start_marker,external_id,input_version_id,input_archive_sha256 FROM build_jobs WHERE submission_id=?",
        (reserved["submission_id"],),
    ).fetchone()
    connection.close()
    assert dict(row) == {
        "start_marker": "persisted-marker",
        "external_id": "accepted-build",
        "input_version_id": "version-1",
        "input_archive_sha256": "a" * 64,
    }


def test_remote_build_persists_input_anchors_before_start(tmp_path):
    database = tmp_path / "remote-anchors.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo-ping", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()

    class AnchoredRemoteBuild:
        provider = "codebuild"
        harness_sha256 = "harness"

        def reconcile(self, marker, window_seconds):
            return []

        def prepare_input(self, request):
            return {"input_version_id": "version-durable", "input_archive_sha256": "d" * 64}

        def start(self, request, expected=None):
            stored = connect(database)
            try:
                row = stored.execute(
                    "SELECT input_version_id,input_archive_sha256 FROM build_jobs WHERE start_marker=?",
                    (request.marker,),
                ).fetchone()
            finally:
                stored.close()
            assert dict(row) == expected
            return BuildHandle("anchored-build", request.marker)

        def poll(self, handle):
            return BuildPoll("succeeded")

        def fetch_outputs(self, handle, expected):
            assert expected["input_version_id"] == "version-durable"
            assert expected["input_archive_sha256"] == "d" * 64
            return BuildOutputs(True, "harness", ())

    remote = AnchoredRemoteBuild()
    providers = SimpleNamespace(build=remote, review_base_url="http://127.0.0.1:5173")
    saga = SubmissionSaga(database, tmp_path / "runtime", providers, heartbeat_interval=0.01)
    reserved, _ = saga.reserve(
        slug="demo-ping", actor="alice", title="Demo", description="Description",
        idempotency_key="remote-anchor-key",
    )
    connection = connect(database)
    generation = claim_job(connection, reserved["submission_id"], "anchor-worker", lease_seconds=60)
    connection.commit()
    connection.close()
    result = saga._execute_remote_with_heartbeat(
        BuildRequest("demo-ping", tmp_path, tmp_path / "out", 5, "input", "anchor-marker"),
        reserved["submission_id"], "anchor-worker", generation,
    )
    assert result.ok


def test_lease_loss_cancels_active_local_build_before_return(tmp_path, monkeypatch):
    stopped = threading.Event()

    class CancellableBuild:
        provider = "local-subprocess"

        def execute(self, request):
            assert request.cancellation_event is not None
            request.cancellation_event.wait(5)
            stopped.set()
            raise RuntimeError("cancelled")

    saga = SubmissionSaga(tmp_path / "unused.sqlite3", tmp_path, SimpleNamespace(build=CancellableBuild(), review_base_url="http://127.0.0.1:5173"), heartbeat_interval=0.01)
    monkeypatch.setattr(saga, "_heartbeat", lambda *args: (_ for _ in ()).throw(LeaseLost("adopted")))
    with pytest.raises(LeaseLost):
        saga._execute_local_with_heartbeat(BuildRequest("demo", tmp_path, tmp_path / "out"), "sub", "worker", 1)
    assert stopped.is_set()
