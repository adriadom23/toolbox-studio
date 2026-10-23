from __future__ import annotations

import json
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineRepository:
    def __init__(self, connection) -> None:
        self.connection = connection

    def get(self, submission_id: str):
        return self.connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()

    def next_version(self, slug: str) -> int:
        row = self.connection.execute("SELECT COALESCE(MAX(version_seq),0)+1 FROM submission_records WHERE project_slug=?", (slug,)).fetchone()
        return int(row[0])

    def insert_building(
        self, *, submission_id: str, slug: str, version: int, actor: str, title: str,
        description: str, correlation_id: str | None, predecessor_id: str | None = None,
        build_provider: str = "local-subprocess", actor_roles: tuple[str, ...] = (),
        author_display: str | None = None,
    ) -> None:
        now = now_iso()
        self.connection.execute(
            "INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,predecessor_id,correlation_id,created_at,updated_at) VALUES (?,?,?,?, 'building',?,?,?,?,?,?,?,?)",
            (submission_id, slug, version, f"v{version}", actor, author_display or actor, title, description, predecessor_id, correlation_id, now, now),
        )
        build_id = "bld_" + submission_id.removeprefix("sub_")
        self.connection.execute("INSERT INTO submission_builds(id,submission_id,provider,status,started_at) VALUES (?,?,?,'reserved',?)", (build_id, submission_id, build_provider, now))
        self.connection.execute("INSERT INTO build_jobs(submission_id,status) VALUES (?,'queued')", (submission_id,))
        self.append_event(submission_id, "build_reserved", actor, {"version": version, "predecessor_id": predecessor_id}, actor_roles=actor_roles, old_state=None, new_state="building", correlation_id=correlation_id)

    def append_event(
        self, submission_id: str, event_type: str, actor: str, data: dict[str, object] | None = None,
        *, actor_roles: tuple[str, ...] = (), old_state: str | None = None,
        new_state: str | None = None, correlation_id: str | None = None,
    ) -> None:
        seq = int(self.connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM submission_operation_events WHERE submission_id=?", (submission_id,)).fetchone()[0])
        self.connection.execute(
            "INSERT INTO submission_operation_events(submission_id,seq,event_type,actor_id,data_json,created_at,actor_roles_json,old_state,new_state,correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (submission_id, seq, event_type, actor, json.dumps(data or {}, sort_keys=True), now_iso(), json.dumps(sorted(actor_roles)), old_state, new_state, correlation_id),
        )

    def mark_failed(self, submission_id: str, code: str) -> None:
        now = now_iso()
        row = self.get(submission_id)
        self.connection.execute("UPDATE submission_records SET state='build_failed',error_code=?,row_version=row_version+1,updated_at=? WHERE id=? AND state='building'", (code, now, submission_id))
        self.connection.execute("UPDATE submission_builds SET status='failed',error_code=?,finished_at=? WHERE submission_id=?", (code, now, submission_id))
        self.append_event(submission_id, "build_failed", "system", {"code": code}, actor_roles=("system",), old_state=row["state"] if row else None, new_state="build_failed", correlation_id=row["correlation_id"] if row else None)

    def mark_built(self, submission_id: str, input_sha: str, harness_sha: str) -> None:
        self.connection.execute("UPDATE submission_records SET input_sha256=?,harness_sha256=?,updated_at=? WHERE id=? AND state='building'", (input_sha, harness_sha, now_iso(), submission_id))
        self.connection.execute("UPDATE submission_builds SET status='succeeded',input_sha256=?,harness_sha256=?,finished_at=? WHERE submission_id=?", (input_sha, harness_sha, now_iso(), submission_id))

    def commit_artifact(self, submission_id: str, *, object_id: str, ref: str, sha256: str, size: int, manifest: dict[str, object], artifact_provider: str = "filesystem") -> None:
        now = now_iso()
        row = self.get(submission_id)
        object_cursor = self.connection.execute(
            "UPDATE artifact_objects SET state='committed',ref=?,provider=?,updated_at=? WHERE object_id=? AND expected_sha256=? AND expected_size=? AND state IN ('reserved','stored','uncertain')",
            (ref, artifact_provider, now, object_id, sha256, size),
        )
        if object_cursor.rowcount != 1:
            raise RuntimeError("artifact reservation missing or inconsistent")
        cursor = self.connection.execute(
            "UPDATE submission_records SET state='submitted',artifact_ref=?,artifact_provider=?,artifact_sha256=?,artifact_size=?,manifest_json=?,row_version=row_version+1,updated_at=? WHERE id=? AND state='building'",
            (ref, artifact_provider, sha256, size, json.dumps(manifest, sort_keys=True, separators=(",", ":")), now, submission_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("submission state changed before artifact commit")
        self.append_event(submission_id, "submitted", "system", {"artifact_sha256": sha256, "artifact_size": size}, actor_roles=("system",), old_state="building", new_state="submitted", correlation_id=row["correlation_id"] if row else None)

    def as_dict(self, row) -> dict[str, object]:
        return {key: row[key] for key in row.keys()}
