from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.submissions.database import connect, transaction
from app.submissions.local_providers import LocalNotificationProvider, LocalPublicationProvider
from app.submissions.migrations import migrate
import app.submissions.outbox as outbox_module
from app.submissions.outbox import OutboxDispatcher, enqueue_effect
from app.submissions.publication import PublicationService
from app.submissions.repository import SubmissionRepository
from app.submissions.repository_v2 import now_iso


class FailOncePublication(LocalPublicationProvider):
    def __init__(self, root):
        super().__init__(root)
        self.failed = False

    def publish(self, effect_id, generation, submission, target):
        if not self.failed:
            self.failed = True
            from app.submissions.providers import PublicationResult
            return PublicationResult(False)
        return super().publish(effect_id, generation, submission, target)


def test_publish_failure_preserves_approved_and_retry_is_idempotent(tmp_path):
    database = tmp_path / "db.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        connection.execute("INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,artifact_ref,artifact_sha256,artifact_size,manifest_json,created_at,updated_at) VALUES ('sub_11111111111111111111111111111111','demo',1,'v1','approved','alice','alice','T','D','immutable-ref',?,1,'{}',?,?)", ("a" * 64, now, now))
    connection.close()
    publication = FailOncePublication(tmp_path / "published")
    providers = SimpleNamespace(publication=publication, notifications=LocalNotificationProvider(tmp_path / "audit.jsonl"))
    service = PublicationService(database, providers)
    failed = service.publish("sub_11111111111111111111111111111111", target="local-index", expected_row_version=1, actor="admin", roles={"admin"}, correlation_id="corr-publish")
    assert failed["effect_status"] == "failed" and failed["status"] == "approved"
    recovered = service.retry(
        "sub_11111111111111111111111111111111",
        failed["effect_id"],
        expected_generation=1,
        actor="admin-b",
        roles={"admin"},
        correlation_id="corr-retry",
    )
    assert recovered["effect_status"] == "delivered" and recovered["status"] == "published"
    assert len(list((tmp_path / "published").glob("*.json"))) == 1
    connection = connect(database)
    event = connection.execute("SELECT actor_id,actor_roles_json,correlation_id,old_state,new_state,data_json FROM submission_operation_events WHERE event_type='published'").fetchone()
    retry_event = connection.execute("SELECT actor_id,actor_roles_json,correlation_id,generation FROM effect_retry_events WHERE effect_id=?", (failed["effect_id"],)).fetchone()
    connection.close()
    event_data = json.loads(event["data_json"])
    assert {key: event[key] for key in ("actor_id", "actor_roles_json", "correlation_id", "old_state", "new_state")} == {
        "actor_id": "admin-b",
        "actor_roles_json": '["admin"]',
        "correlation_id": "corr-retry",
        "old_state": "approved",
        "new_state": "published",
    }
    assert event_data == {
        "effect_id": failed["effect_id"],
        "origin_actor_id": "admin",
        "reference": f"{failed['effect_id']}.json",
        "target": "local-index",
    }
    assert dict(retry_event) == {"actor_id": "admin-b", "actor_roles_json": '["admin"]', "correlation_id": "corr-retry", "generation": 2}


def test_tampered_review_url_is_revalidated_before_notification_send(tmp_path):
    database = tmp_path / "tampered-url.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        connection.execute("INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,created_at,updated_at) VALUES ('sub_33333333333333333333333333333333','demo',1,'v1','building','alice','alice','T','D',?,?)", (now, now))
        effect_id = enqueue_effect(connection, "sub_33333333333333333333333333333333", "notification", {"project": "demo", "version": "v1", "short_id": "33333333", "author": "alice", "state": "submitted", "review_url": "https://studio.example/submissions/3?token=secret"})

    class Notifier:
        calls = 0

        def send(self, *_args):
            self.calls += 1
            raise AssertionError("unsafe URL reached notification provider")

    notifier = Notifier()
    providers = SimpleNamespace(
        notifications=notifier,
        publication=LocalPublicationProvider(tmp_path / "published"),
        review_base_url="https://studio.example",
    )
    result = OutboxDispatcher(connection, providers).dispatch(effect_id)
    connection.close()
    assert result["status"] == "delivery_unknown"
    assert notifier.calls == 0


def test_ambiguous_notification_is_not_automatically_resent(tmp_path):
    database = tmp_path / "ambiguous.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        connection.execute("INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,created_at,updated_at) VALUES ('sub_22222222222222222222222222222222','demo',1,'v1','building','alice','alice','T','D',?,?)", (now, now))
        effect_id = enqueue_effect(connection, "sub_22222222222222222222222222222222", "notification", {"project": "demo", "version": "v1", "short_id": "22222222", "author": "alice", "state": "submitted", "review_url": "https://studio.example/submissions/2"})

    class DeliveredThenCrashed:
        calls = 0
        def send(self, *_args):
            self.calls += 1
            raise RuntimeError("crash after provider acceptance")

    notifier = DeliveredThenCrashed()
    providers = SimpleNamespace(notifications=notifier, publication=LocalPublicationProvider(tmp_path / "published"))
    first = OutboxDispatcher(connection, providers).dispatch(effect_id)
    assert first["status"] == "delivery_unknown" and notifier.calls == 1
    assert OutboxDispatcher(connection, providers).dispatch_pending() == []
    assert notifier.calls == 1
    retried = OutboxDispatcher(connection, providers).retry(
        effect_id,
        1,
        actor="admin-b",
        roles={"admin"},
        correlation_id="notification-retry",
    )
    assert retried["generation"] == 2 and notifier.calls == 2
    retry_event = connection.execute(
        "SELECT actor_id,actor_roles_json,correlation_id FROM effect_retry_events WHERE effect_id=? AND generation=2",
        (effect_id,),
    ).fetchone()
    assert dict(retry_event) == {"actor_id": "admin-b", "actor_roles_json": '["admin"]', "correlation_id": "notification-retry"}
    connection.close()


def test_notification_channels_persist_partial_delivery_and_retry_only_failed_channel(tmp_path):
    database = tmp_path / "notification-channels.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        submission_id = "sub_" + "5" * 32
        connection.execute(
            "INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,created_at,updated_at) "
            "VALUES (?,?,1,'v1','building','alice','alice','T','D',?,?)",
            (submission_id, "demo", now, now),
        )
        effect_ids = outbox_module.enqueue_notification(
            connection,
            connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone(),
            state="submitted",
            review_base_url="https://studio.example",
            channels=("ses", "teams"),
        )

    class DeliveredSes:
        calls = 0

        def send(self, *_args):
            from app.submissions.providers import ProviderResult

            self.calls += 1
            return ProviderResult(True, "ses-message")

    class FailsOnceTeams:
        calls = 0

        def send(self, *_args):
            from app.submissions.providers import ProviderResult

            self.calls += 1
            return ProviderResult(self.calls > 1, "teams-message" if self.calls > 1 else None)

    ses = DeliveredSes()
    teams = FailsOnceTeams()
    providers = SimpleNamespace(
        notifications={"ses": ses, "teams": teams},
        publication=LocalPublicationProvider(tmp_path / "published"),
        review_base_url="https://studio.example",
    )
    dispatcher = OutboxDispatcher(connection, providers)
    first = [dispatcher.dispatch(effect_id) for effect_id in effect_ids]
    by_channel = {item["provider_key"]: item for item in first}
    assert by_channel["ses"]["status"] == "delivered"
    assert by_channel["teams"]["status"] == "failed"
    assert ses.calls == 1 and teams.calls == 1

    retried = dispatcher.retry(
        by_channel["teams"]["id"],
        1,
        actor="admin",
        roles={"admin"},
        correlation_id="retry-teams",
    )
    assert retried["status"] == "delivered"
    assert ses.calls == 1 and teams.calls == 2
    rows = connection.execute(
        "SELECT provider_key,status FROM outbox_effects WHERE submission_id=? ORDER BY provider_key",
        (submission_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [("ses", "delivered"), ("teams", "delivered")]
    connection.close()


def test_retry_rolls_back_effect_state_when_audit_insert_boundary_fails(tmp_path, monkeypatch):
    database = tmp_path / "retry-atomic.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        connection.execute(
            "INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,created_at,updated_at) "
            "VALUES ('sub_44444444444444444444444444444444','demo',1,'v1','building','alice','alice','T','D',?,?)",
            (now, now),
        )
        effect_id = enqueue_effect(
            connection,
            "sub_44444444444444444444444444444444",
            "notification",
            {"project": "demo", "version": "v1", "short_id": "44444444", "author": "alice", "state": "submitted", "review_url": "https://studio.example/submissions/4"},
        )
        connection.execute("UPDATE outbox_effects SET status='failed' WHERE id=?", (effect_id,))

    providers = SimpleNamespace(
        notifications=LocalNotificationProvider(tmp_path / "audit.jsonl"),
        publication=LocalPublicationProvider(tmp_path / "published"),
    )
    monkeypatch.setattr(outbox_module, "_before_retry_audit", lambda: (_ for _ in ()).throw(RuntimeError("fault after retry CAS")))
    with pytest.raises(RuntimeError, match="fault after retry CAS"):
        OutboxDispatcher(connection, providers).retry(
            effect_id,
            1,
            actor="admin",
            roles={"admin"},
            correlation_id="retry-atomic",
        )
    effect = connection.execute("SELECT generation,status FROM outbox_effects WHERE id=?", (effect_id,)).fetchone()
    assert dict(effect) == {"generation": 1, "status": "failed"}
    assert connection.execute("SELECT COUNT(*) FROM effect_retry_events WHERE effect_id=?", (effect_id,)).fetchone()[0] == 0
    assert not (tmp_path / "audit.jsonl").exists()
    connection.close()


def test_delivered_publication_reconciles_after_fault_between_commits(tmp_path, monkeypatch):
    database = tmp_path / "publication-reconcile.sqlite3"
    migrate(database)
    connection = connect(database)
    submission_id = "sub_" + "6" * 32
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        connection.execute(
            "INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,artifact_ref,artifact_sha256,artifact_size,manifest_json,created_at,updated_at) "
            "VALUES (?, 'demo',1,'v1','approved','alice','alice','T','D','immutable-ref',?,1,'{}',?,?)",
            (submission_id, "a" * 64, now, now),
        )
        effect_id = enqueue_effect(
            connection,
            submission_id,
            "publication",
            {"target": "local-index", "actor_id": "admin", "actor_roles": ["admin"], "correlation_id": "fault-boundary"},
        )

    class CountingPublication(LocalPublicationProvider):
        calls = 0

        def publish(self, *args):
            self.calls += 1
            return super().publish(*args)

    publication = CountingPublication(tmp_path / "published")
    providers = SimpleNamespace(
        notifications=LocalNotificationProvider(tmp_path / "audit.jsonl"),
        publication=publication,
    )
    monkeypatch.setattr(outbox_module, "_after_effect_delivery_commit", lambda: (_ for _ in ()).throw(RuntimeError("fault after delivered commit")))
    with pytest.raises(RuntimeError, match="fault after delivered commit"):
        OutboxDispatcher(connection, providers).dispatch(effect_id)
    before = connection.execute("SELECT state FROM submission_records WHERE id=?", (submission_id,)).fetchone()
    effect = connection.execute("SELECT status FROM outbox_effects WHERE id=?", (effect_id,)).fetchone()
    assert before["state"] == "approved" and effect["status"] == "delivered"
    assert publication.calls == 1

    assert OutboxDispatcher(connection, providers).dispatch_pending() == []
    after = connection.execute("SELECT state FROM submission_records WHERE id=?", (submission_id,)).fetchone()
    events = connection.execute(
        "SELECT COUNT(*) FROM submission_operation_events WHERE submission_id=? AND event_type='published'",
        (submission_id,),
    ).fetchone()[0]
    assert after["state"] == "published" and events == 1
    assert publication.calls == 1
    assert OutboxDispatcher(connection, providers).dispatch_pending() == []
    assert connection.execute(
        "SELECT COUNT(*) FROM submission_operation_events WHERE submission_id=? AND event_type='published'",
        (submission_id,),
    ).fetchone()[0] == 1
    connection.close()


def test_retry_rejects_active_attempt_and_allows_expired_lease(tmp_path):
    database = tmp_path / "retry-in-flight.sqlite3"
    migrate(database)
    seed_connection = connect(database)
    submission_id = "sub_" + "7" * 32
    with transaction(seed_connection):
        SubmissionRepository(seed_connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        now = now_iso()
        seed_connection.execute(
            "INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,created_at,updated_at) "
            "VALUES (?, 'demo',1,'v1','building','alice','alice','T','D',?,?)",
            (submission_id, now, now),
        )
        effect_id = enqueue_effect(
            seed_connection,
            submission_id,
            "notification",
            {"project": "demo", "version": "v1", "short_id": "77777777", "author": "alice", "state": "submitted", "review_url": "https://studio.example/submissions/7"},
        )
    seed_connection.close()

    started = threading.Event()
    release = threading.Event()

    class BlockingNotifier:
        calls = 0

        def send(self, *_args):
            from app.submissions.providers import ProviderResult

            self.calls += 1
            started.set()
            assert release.wait(timeout=5)
            return ProviderResult(True, "message")

    notifier = BlockingNotifier()
    providers = SimpleNamespace(
        notifications=notifier,
        publication=LocalPublicationProvider(tmp_path / "published"),
        review_base_url="https://studio.example",
    )
    dispatch_errors = []

    def run_dispatch():
        connection = connect(database)
        try:
            OutboxDispatcher(connection, providers, worker_id="worker-active").dispatch(effect_id)
        except Exception as exc:  # pragma: no cover - assertion reports the captured error
            dispatch_errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=run_dispatch)
    thread.start()
    assert started.wait(timeout=5)
    retry_connection = connect(database)
    with pytest.raises(outbox_module.PublicError) as active:
        OutboxDispatcher(retry_connection, providers).retry(
            effect_id, 1, actor="admin", roles={"admin"}, correlation_id="retry-active",
        )
    assert active.value.code == "effect_in_flight"
    assert active.value.as_dict("corr") == {
        "error": "effect_in_flight",
        "message": "effect delivery is already in progress",
        "recoverable": True,
        "correlation_id": "corr",
        "details": {
            "status": "in_progress",
            "retry_after": active.value.details["retry_after"],
            "poll_url": f"/api/submissions/{submission_id}",
        },
    }
    assert 1 <= active.value.details["retry_after"] <= 60
    assert notifier.calls == 1
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and not dispatch_errors and notifier.calls == 1

    with transaction(retry_connection):
        retry_connection.execute(
            "UPDATE outbox_effects SET status='delivery_unknown',lease_owner='dead-worker',lease_until=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), effect_id),
        )
        retry_connection.execute(
            "INSERT INTO effect_attempts(effect_id,generation,attempt,status,error_code,created_at) VALUES (?,1,99,'sending','notification_delivery_unknown',?)",
            (effect_id, now_iso()),
        )

    class RecoveredNotifier:
        calls = 0

        def send(self, *_args):
            from app.submissions.providers import ProviderResult

            self.calls += 1
            return ProviderResult(True, "recovered")

    recovered_notifier = RecoveredNotifier()
    recovered = OutboxDispatcher(
        retry_connection,
        SimpleNamespace(
            notifications=recovered_notifier,
            publication=providers.publication,
            review_base_url="https://studio.example",
        ),
    ).retry(effect_id, 1, actor="admin", roles={"admin"}, correlation_id="retry-expired")
    assert recovered["generation"] == 2 and recovered["status"] == "delivered"
    assert recovered_notifier.calls == 1
    retry_connection.close()
