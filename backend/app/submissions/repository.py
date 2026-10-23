from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.errors import ConflictError
from app.submissions.models import ProjectRecord, SubmissionRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubmissionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def upsert_project(self, *, slug: str, display_name: str, created_at: str | None, modified_at: str | None, status: str = "draft", source: str = "filesystem") -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO projects(slug, display_name, created_at, modified_at, status, source) VALUES (?, ?, ?, ?, ?, ?)",
            (slug, display_name, created_at, modified_at, status, source),
        )
        return cursor.rowcount == 1

    def get_project(self, slug: str) -> ProjectRecord | None:
        row = self.connection.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        return ProjectRecord(**dict(row)) if row else None

    def create_submission(self, project_slug: str, actor: str) -> SubmissionRecord:
        created_at = _now()
        cursor = self.connection.execute(
            "INSERT INTO submissions(project_slug, status, created_by, created_at) VALUES (?, 'submitted', ?, ?)",
            (project_slug, actor, created_at),
        )
        self.append_event(project_slug=project_slug, submission_id=cursor.lastrowid, event_type="submitted", actor=actor)
        return self.get_submission(int(cursor.lastrowid))

    def get_submission(self, submission_id: int) -> SubmissionRecord | None:
        row = self.connection.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        return SubmissionRecord(**dict(row)) if row else None

    def compare_and_set_submission(self, submission_id: int, expected_version: int, *, status: str, package_path: str | None = None, package_sha256: str | None = None) -> SubmissionRecord:
        cursor = self.connection.execute(
            "UPDATE submissions SET status = ?, package_path = COALESCE(?, package_path), package_sha256 = COALESCE(?, package_sha256), version = version + 1 WHERE id = ? AND version = ?",
            (status, package_path, package_sha256, submission_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise ConflictError()
        return self.get_submission(submission_id)

    def append_event(self, *, project_slug: str, event_type: str, actor: str, submission_id: int | None = None, payload: dict[str, Any] | None = None, dedupe_key: str | None = None) -> bool:
        try:
            self.connection.execute(
                "INSERT INTO submission_events(submission_id, project_slug, event_type, actor, payload_json, created_at, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (submission_id, project_slug, event_type, actor, json.dumps(payload or {}, sort_keys=True), _now(), dedupe_key),
            )
        except sqlite3.IntegrityError:
            if dedupe_key is not None:
                return False
            raise
        return True

    def claim_local(self, claim_key: str, identity: str) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO local_claims(claim_key, identity, claimed_at) VALUES (?, ?, ?)",
            (claim_key, identity, _now()),
        )
        return cursor.rowcount == 1
