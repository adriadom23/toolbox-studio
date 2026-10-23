from __future__ import annotations

from app.core.errors import PublicError
from app.submissions.models import SubmissionRecord
from app.submissions.repository import SubmissionRepository

_ALLOWED = {
    "submitted": {"in_review"},
    "in_review": {"approved", "rejected"},
    "approved": {"published"},
    "rejected": {"in_review"},
    "published": set(),
}


class SubmissionDomain:
    def __init__(self, repository: SubmissionRepository) -> None:
        self.repository = repository

    def transition(self, submission_id: int, expected_version: int, target: str, actor: str) -> SubmissionRecord:
        current = self.repository.get_submission(submission_id)
        if current is None:
            raise PublicError("not_found", "submission not found", 404)
        if target not in _ALLOWED.get(current.status, set()):
            raise PublicError("invalid_transition", f"cannot transition {current.status} to {target}", 409)
        updated = self.repository.compare_and_set_submission(submission_id, expected_version, status=target)
        self.repository.append_event(project_slug=current.project_slug, submission_id=submission_id, event_type=target, actor=actor)
        return updated

    def comment(self, submission_id: int, actor: str, text: str) -> None:
        current = self.repository.get_submission(submission_id)
        if current is None:
            raise PublicError("not_found", "submission not found", 404)
        if not text.strip():
            raise PublicError("invalid_comment", "comment must not be empty", 400)
        self.repository.append_event(project_slug=current.project_slug, submission_id=submission_id, event_type="commented", actor=actor, payload={"text": text})
