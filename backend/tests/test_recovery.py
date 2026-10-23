from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.submissions.database import connect, transaction
from app.submissions.migrations import migrate
from app.submissions.local_providers import LocalNotificationProvider, LocalPublicationProvider
from app.submissions.outbox import OutboxDispatcher
from app.submissions.recovery import ArtifactRecoveryUnavailable, recover_interrupted_submissions
from app.submissions.repository import SubmissionRepository
from app.submissions.repository_v2 import PipelineRepository
from app.submissions.runtime_refs import normalize_runtime_references, resolve_runtime_ref
from app.submissions.stores import FilesystemArtifactStore, sha256_file


def test_absolute_runtime_references_are_migrated_to_confined_logical_refs(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    snapshot = runtime / "inputs" / "sealed"
    output = runtime / "builds" / "sealed"
    snapshot.mkdir(parents=True)
    output.mkdir(parents=True)
    database = runtime / "refs.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(
            submission_id="sub_" + "3" * 32, slug="demo", version=1, actor="a",
            title="t", description="d", correlation_id=None,
        )
        connection.execute(
            "UPDATE submission_records SET snapshot_ref=?,build_output_ref=?",
            (str(snapshot), str(output)),
        )
    connection.close()
    assert normalize_runtime_references(database, runtime) == 2
    connection = connect(database)
    refs = connection.execute("SELECT snapshot_ref,build_output_ref FROM submission_records").fetchone()
    connection.close()
    assert refs["snapshot_ref"] == "runtime:v1:inputs/sealed"
    assert refs["build_output_ref"] == "runtime:v1:builds/sealed"
    assert resolve_runtime_ref(runtime, refs["snapshot_ref"]) == snapshot.resolve()
    assert resolve_runtime_ref(runtime, refs["build_output_ref"]) == output.resolve()


def test_recovery_resumes_early_phase_with_same_operation_and_version(tmp_path):
    database = tmp_path / "db.sqlite3"
    migrate(database)
    submission_id = "sub_" + "1" * 32
    operation_id = "op_" + "1" * 32
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(submission_id=submission_id, slug="demo", version=1, actor="a", title="t", description="d", correlation_id=None)
        connection.execute(
            "INSERT INTO idempotency_records(scope,actor_id,route_template,resource_id,key,request_sha256,status,operation_id,subject_id,expires_at) "
            "VALUES ('submission','a','/api/submissions','demo','recovery-key','hash','pending',?,?,datetime('now','+1 day'))",
            (operation_id, submission_id),
        )
    connection.close()
    calls = []
    assert recover_interrupted_submissions(
        database, resume_callback=lambda operation, submission: calls.append((operation, submission))
    ) == [submission_id]
    assert calls == [(operation_id, submission_id)]
    connection = connect(database)
    row = connection.execute("SELECT state,version_seq FROM submission_records").fetchone()
    assert dict(row) == {"state": "building", "version_seq": 1}
    connection.close()


def test_recovery_adopts_verified_object_after_put_before_commit(tmp_path):
    database = tmp_path / "adopt.sqlite3"
    migrate(database)
    submission_id = "sub_" + "2" * 32
    object_id = "art:v1:" + submission_id
    source = tmp_path / "artifact.zip"
    source.write_bytes(b"verified package")
    size, digest = sha256_file(source)
    store = FilesystemArtifactStore(tmp_path / "objects")
    store.put_if_absent(object_id, source, size, digest)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(submission_id=submission_id, slug="demo", version=1, actor="a", title="t", description="d", correlation_id=None)
        connection.execute(
            "INSERT INTO artifact_objects(object_id,expected_sha256,expected_size,state,provider,submission_id,manifest_json,input_sha256,harness_sha256,created_at,updated_at) VALUES (?,?,?,'reserved','filesystem',?,'{}','i','h',datetime('now'),datetime('now'))",
            (object_id, digest, size, submission_id),
        )
        connection.execute(
            "INSERT INTO idempotency_records(scope,actor_id,route_template,resource_id,key,request_sha256,status,operation_id,subject_id,expires_at) "
            "VALUES ('submission','a','/api/projects/{slug}/submit','demo','legacy-recovery','hash','pending',?,?,datetime('now','+1 day'))",
            ("op_" + "2" * 32, submission_id),
        )
    connection.close()
    assert recover_interrupted_submissions(database, store) == [submission_id]
    connection = connect(database)
    row = connection.execute("SELECT state,artifact_sha256 FROM submission_records WHERE id=?", (submission_id,)).fetchone()
    assert dict(row) == {"state": "submitted", "artifact_sha256": digest}
    assert connection.execute("SELECT count(*) FROM outbox_effects WHERE submission_id=?", (submission_id,)).fetchone()[0] == 1
    replay = connection.execute("SELECT status,status_code,response_json FROM idempotency_records WHERE subject_id=?", (submission_id,)).fetchone()
    assert dict(replay) == {"status": "pending", "status_code": None, "response_json": None}
    notify = connection.execute(
        "SELECT status FROM submission_phases WHERE submission_id=? AND phase='notify'",
        (submission_id,),
    ).fetchone()
    assert notify["status"] == "running"
    providers = SimpleNamespace(
        notifications={"local": LocalNotificationProvider(tmp_path / "notifications.jsonl")},
        publication=LocalPublicationProvider(tmp_path / "published"),
        review_base_url="http://127.0.0.1:5173",
    )
    assert OutboxDispatcher(connection, providers).dispatch_pending()
    replay = connection.execute("SELECT status,status_code,response_json FROM idempotency_records WHERE subject_id=?", (submission_id,)).fetchone()
    assert replay["status"] == "completed" and replay["status_code"] == 200
    payload = json.loads(replay["response_json"])
    assert payload["notified"] == {"email": False, "slack": False}
    assert payload["meta"]["name"] == "demo"
    assert payload["submission"]["submission_id"] == submission_id
    assert payload["notifications"] == [{"provider_key": "local", "status": "delivered", "error_code": None}]
    assert next(phase for phase in payload["phases"] if phase["phase"] == "notify")["status"] == "succeeded"
    connection.close()


def test_recovery_retries_transient_artifact_stat_without_terminal_failure(tmp_path):
    database = tmp_path / "transient.sqlite3"
    migrate(database)
    submission_id = "sub_" + "4" * 32
    object_id = "art:v1:" + submission_id
    source = tmp_path / "artifact.zip"
    source.write_bytes(b"verified package")
    size, digest = sha256_file(source)
    store = FilesystemArtifactStore(tmp_path / "objects")
    store.put_if_absent(object_id, source, size, digest)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(
            submission_id=submission_id, slug="demo", version=1, actor="a",
            title="t", description="d", correlation_id=None,
        )
        connection.execute(
            "INSERT INTO artifact_objects(object_id,expected_sha256,expected_size,state,provider,submission_id,manifest_json,input_sha256,harness_sha256,created_at,updated_at) "
            "VALUES (?,?,?,'stored','filesystem',?,'{}','i','h',datetime('now'),datetime('now'))",
            (object_id, digest, size, submission_id),
        )
    connection.close()

    class FailsOnce:
        def __init__(self, delegate):
            self.delegate = delegate
            self.calls = 0

        def resolve_existing(self, object_id, size, sha256):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary artifact timeout")
            return self.delegate.resolve_existing(object_id, size, sha256)

        def stat(self, ref):
            return self.delegate.stat(ref)

    flaky = FailsOnce(store)
    with pytest.raises(ArtifactRecoveryUnavailable):
        recover_interrupted_submissions(database, flaky)
    connection = connect(database)
    assert connection.execute("SELECT state FROM submission_records WHERE id=?", (submission_id,)).fetchone()[0] == "building"
    assert connection.execute("SELECT status,last_error_code FROM build_jobs WHERE submission_id=?", (submission_id,)).fetchone()[:] == ("pending", "artifact_store_unavailable")
    connection.close()

    assert recover_interrupted_submissions(database, flaky) == [submission_id]
    connection = connect(database)
    assert connection.execute("SELECT state FROM submission_records WHERE id=?", (submission_id,)).fetchone()[0] == "submitted"
    connection.close()
