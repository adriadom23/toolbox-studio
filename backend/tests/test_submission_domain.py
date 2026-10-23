from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.errors import ConflictError, PublicError  # noqa: E402
from app.submissions.database import connect  # noqa: E402
from app.submissions.domain import SubmissionDomain  # noqa: E402
from app.submissions.migrations import migrate  # noqa: E402
from app.submissions.repository import SubmissionRepository  # noqa: E402


def test_domain_transitions_use_cas_and_append_events(tmp_path):
    path = tmp_path / "studio.sqlite3"
    migrate(path)
    connection = connect(path)
    repo = SubmissionRepository(connection)
    repo.upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    submission = repo.create_submission("demo", "alice")
    updated = SubmissionDomain(repo).transition(submission.id, 1, "in_review", "owner")
    assert updated.status == "in_review" and updated.version == 2
    with pytest.raises(ConflictError):
        SubmissionDomain(repo).transition(submission.id, 1, "approved", "owner")
    assert connection.execute("SELECT count(*) FROM submission_events").fetchone()[0] == 2
    connection.close()


def test_invalid_transition_is_rejected(tmp_path):
    path = tmp_path / "studio.sqlite3"
    migrate(path)
    connection = connect(path)
    repo = SubmissionRepository(connection)
    repo.upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    submission = repo.create_submission("demo", "alice")
    with pytest.raises(PublicError, match="cannot transition"):
        SubmissionDomain(repo).transition(submission.id, 1, "published", "owner")
    connection.close()
