from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.errors import PublicError
from app.submissions.database import connect, transaction
from app.submissions.migrations import migrate
from app.submissions.local_providers import LocalNotificationProvider, LocalPublicationProvider
from app.submissions.repository import SubmissionRepository
from app.submissions.repository_v2 import PipelineRepository, now_iso
from app.submissions.review import ReviewService


def seed(database: Path, author: str = "alice") -> str:
    migrate(database)
    connection = connect(database)
    try:
        with transaction(connection):
            SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
            submission_id = "sub_" + "1" * 32
            now = now_iso()
            connection.execute("INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,artifact_ref,artifact_sha256,artifact_size,manifest_json,created_at,updated_at) VALUES (?,?,1,'v1','submitted',?,?,?,?,?,?,?, '{}',?,?)", (submission_id, "demo", author, author, "Title", "Description", "ref", "a" * 64, 1, now, now))
            PipelineRepository(connection).append_event(submission_id, "submitted", "system")
            return submission_id
    finally:
        connection.close()


def test_review_matrix_reasons_cas_and_append_only_comments(tmp_path):
    database = tmp_path / "review.sqlite3"
    submission_id = seed(database)
    service = ReviewService(database)
    with pytest.raises(PublicError) as denied:
        service.transition(submission_id, action="start", expected_row_version=1, actor="alice", roles={"submitter"})
    assert denied.value.status_code == 403
    started = service.transition(
        submission_id, action="start", expected_row_version=1, actor="reviewer",
        roles={"reviewer"}, correlation_id="corr-start",
    )
    assert started.state == "under_review" and started.row_version == 2
    with pytest.raises(PublicError) as stale:
        service.transition(submission_id, action="approve", expected_row_version=1, actor="admin", roles={"admin"})
    assert stale.value.code == "stale_submission"
    with pytest.raises(PublicError) as reason:
        service.transition(submission_id, action="request_changes", expected_row_version=2, actor="reviewer", roles={"reviewer"})
    assert reason.value.code == "reason_required"
    comment = service.comment(
        submission_id, actor="alice", roles={"submitter"}, text="  plain <b>text</b>  ",
        correlation_id="corr-comment",
    )
    assert comment["body"] == "plain <b>text</b>"
    approved = service.transition(
        submission_id, action="approve", expected_row_version=2, actor="reviewer",
        roles={"reviewer"}, reason="Looks good", correlation_id="corr-approve",
    )
    assert approved.state == "approved"
    connection = connect(database)
    try:
        comments = connection.execute("SELECT body FROM submission_comments ORDER BY seq").fetchall()
        assert [row["body"] for row in comments] == ["plain <b>text</b>", "Looks good"]
        events = connection.execute(
            "SELECT event_type,actor_roles_json,correlation_id FROM submission_operation_events "
            "WHERE event_type IN ('review_started','comment_added','approved') ORDER BY seq"
        ).fetchall()
        assert [(row["event_type"], row["actor_roles_json"], row["correlation_id"]) for row in events] == [
            ("review_started", '["reviewer"]', "corr-start"),
            ("comment_added", '["submitter"]', "corr-comment"),
            ("approved", '["reviewer"]', "corr-approve"),
        ]
        with pytest.raises(Exception, match="append-only"):
            connection.execute("UPDATE submission_comments SET body='changed'")
    finally:
        connection.close()


def test_approve_local_index_attempts_publication_and_returns_published(tmp_path):
    database = tmp_path / "auto-publish.sqlite3"
    submission_id = seed(database)
    providers = SimpleNamespace(
        notifications=LocalNotificationProvider(tmp_path / "notifications.jsonl"),
        publication=LocalPublicationProvider(tmp_path / "published"),
    )
    service = ReviewService(database, providers=providers)
    started = service.transition(submission_id, action="start", expected_row_version=1, actor="reviewer", roles={"reviewer"})
    result = service.transition(
        submission_id,
        action="approve",
        expected_row_version=started.row_version,
        actor="reviewer",
        roles={"reviewer"},
        publication_target="local-index",
        correlation_id="corr-auto-publish",
    )
    assert result.state == "published"
    connection = connect(database)
    effect = connection.execute("SELECT status,payload_json FROM outbox_effects WHERE submission_id=? AND kind='publication'", (submission_id,)).fetchone()
    event = connection.execute("SELECT actor_id,old_state,new_state,correlation_id FROM submission_operation_events WHERE submission_id=? AND event_type='published'", (submission_id,)).fetchone()
    connection.close()
    assert effect["status"] == "delivered" and '"actor_id":"reviewer"' in effect["payload_json"]
    assert dict(event) == {"actor_id": "reviewer", "old_state": "approved", "new_state": "published", "correlation_id": "corr-auto-publish"}


def test_invisible_submission_returns_not_found(tmp_path):
    database = tmp_path / "hidden.sqlite3"
    submission_id = seed(database)
    with pytest.raises(PublicError) as hidden:
        ReviewService(database).comment(submission_id, actor="mallory", roles={"submitter"}, text="hello")
    assert hidden.value.status_code == 404
