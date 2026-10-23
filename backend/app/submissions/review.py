from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.core.errors import PublicError
from app.submissions.database import connect, transaction
from app.submissions.outbox import OutboxDispatcher, enqueue_effect, enqueue_notification, notification_channels
from app.submissions.repository_v2 import PipelineRepository, now_iso

_REVIEW_ROLES = {"reviewer", "admin"}
_TRANSITIONS = {
    "start": ("submitted", "under_review", "review_started"),
    "request_changes": ("under_review", "changes_requested", "changes_requested"),
    "reject": ("under_review", "rejected", "rejected"),
    "approve": ("under_review", "approved", "approved"),
}


def can_view(row, actor: str, roles: set[str]) -> bool:
    return bool(roles & _REVIEW_ROLES) or row["author_id"] == actor


def require_visible(row, actor: str, roles: set[str]) -> None:
    if row is None or not can_view(row, actor, roles):
        raise PublicError("not_found", "submission not found", 404)


@dataclass(frozen=True)
class ReviewResult:
    submission_id: str
    state: str
    row_version: int


class ReviewService:
    def __init__(self, database_path, *, providers=None, review_base_url: str = "http://127.0.0.1:5173") -> None:
        self.database_path = database_path
        self.providers = providers
        self.review_base_url = review_base_url.rstrip("/")

    def transition(self, submission_id: str, *, action: str, expected_row_version: int, actor: str, roles: set[str], reason: str = "", publication_target: str = "none", correlation_id: str = "") -> ReviewResult:
        if action not in _TRANSITIONS:
            raise PublicError("invalid_review_action", "unknown review action", 422)
        if not roles & _REVIEW_ROLES:
            connection = connect(self.database_path)
            try:
                require_visible(connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone(), actor, roles)
            finally:
                connection.close()
            raise PublicError("forbidden", "review capability required", 403)
        reason = reason.strip()
        if action in {"request_changes", "reject"} and not reason:
            raise PublicError("reason_required", "reason is required", 422)
        if len(reason) > 4000:
            raise PublicError("reason_too_long", "reason must not exceed 4000 characters", 422)
        if action == "approve" and publication_target not in {"none", "local-index"}:
            raise PublicError("invalid_publication_target", "unsupported publication target", 422)

        source, target, event_type = _TRANSITIONS[action]
        connection = connect(self.database_path)
        notification_effect_ids: list[str] = []
        publication_effect_id = ""
        try:
            with transaction(connection):
                row = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
                require_visible(row, actor, roles)
                if int(row["row_version"]) != expected_row_version:
                    raise PublicError("stale_submission", "submission changed", 409)
                if row["state"] != source:
                    raise PublicError("invalid_transition", f"cannot {action} from {row['state']}", 409)
                now = now_iso()
                cursor = connection.execute(
                    "UPDATE submission_records SET state=?,row_version=row_version+1,updated_at=? WHERE id=? AND row_version=? AND state=?",
                    (target, now, submission_id, expected_row_version, source),
                )
                if cursor.rowcount != 1:
                    raise PublicError("stale_submission", "submission changed", 409)
                data: dict[str, object] = {}
                if reason:
                    data["reason"] = reason
                if action == "approve":
                    data["publication_target"] = publication_target
                PipelineRepository(connection).append_event(
                    submission_id, event_type, actor, data,
                    actor_roles=tuple(sorted(roles)), old_state=source, new_state=target,
                    correlation_id=correlation_id,
                )
                if reason and action in {"request_changes", "reject", "approve"}:
                    seq = int(connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM submission_comments WHERE submission_id=?", (submission_id,)).fetchone()[0])
                    connection.execute(
                        "INSERT INTO submission_comments(id,submission_id,seq,author_id,body,created_at) VALUES (?,?,?,?,?,?)",
                        ("cmt_" + secrets.token_hex(16), submission_id, seq, actor, reason, now),
                    )
                updated = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
                notification_effect_ids = enqueue_notification(
                    connection,
                    updated,
                    state=target,
                    review_base_url=self.review_base_url,
                    channels=notification_channels(self.providers) if self.providers is not None else ("local",),
                )
                if action == "approve" and publication_target == "local-index":
                    publication_effect_id = enqueue_effect(
                        connection,
                        submission_id,
                        "publication",
                        {
                            "target": publication_target,
                            "actor_id": actor,
                            "actor_roles": sorted(roles),
                            "correlation_id": correlation_id,
                        },
                    )
                result = ReviewResult(submission_id, target, int(updated["row_version"]))
        finally:
            connection.close()
        if self.providers is not None:
            dispatch_connection = connect(self.database_path)
            try:
                dispatcher = OutboxDispatcher(dispatch_connection, self.providers)
                for notification_effect_id in notification_effect_ids:
                    dispatcher.dispatch(notification_effect_id)
                if publication_effect_id:
                    dispatcher.dispatch(publication_effect_id)
                    updated = dispatch_connection.execute("SELECT state,row_version FROM submission_records WHERE id=?", (submission_id,)).fetchone()
                    result = ReviewResult(submission_id, str(updated["state"]), int(updated["row_version"]))
            except Exception:
                pass
            finally:
                dispatch_connection.close()
        return result

    def comment(self, submission_id: str, *, actor: str, roles: set[str], text: str, correlation_id: str = "") -> dict[str, object]:
        body = text.strip()
        if not body or len(body) > 4000:
            raise PublicError("invalid_comment", "comment must contain 1-4000 characters", 422)
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
                require_visible(row, actor, roles)
                seq = int(connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM submission_comments WHERE submission_id=?", (submission_id,)).fetchone()[0])
                comment_id = "cmt_" + secrets.token_hex(16)
                created_at = now_iso()
                connection.execute(
                    "INSERT INTO submission_comments(id,submission_id,seq,author_id,body,created_at) VALUES (?,?,?,?,?,?)",
                    (comment_id, submission_id, seq, actor, body, created_at),
                )
                PipelineRepository(connection).append_event(
                    submission_id, "comment_added", actor, {"comment_id": comment_id},
                    actor_roles=tuple(sorted(roles)), correlation_id=correlation_id,
                )
                return {"id": comment_id, "submission_id": submission_id, "seq": seq, "author": actor, "body": body, "created_at": created_at}
        finally:
            connection.close()
