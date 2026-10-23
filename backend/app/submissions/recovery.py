from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Callable

from app.submissions.database import connect, transaction
from app.submissions.idempotency import complete
from app.submissions.jobs import claim_job, finish_job, require_job_lease
from app.submissions.outbox import enqueue_notification, reconcile_notification_phase
from app.submissions.providers import ObjectRef
from app.submissions.repository_v2 import PipelineRepository, now_iso


class ArtifactRecoveryUnavailable(RuntimeError):
    code = "artifact_store_unavailable"


def _artifact_stat_failure(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "missing"
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and str(response.get("Error", {}).get("Code", "")) in {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}:
        return "missing"
    if isinstance(exc, (ValueError, FileExistsError)):
        return "conflict"
    return "unavailable"


def _lease_active(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) > datetime.now(timezone.utc)
    except ValueError:
        return False


def recover_interrupted_submissions(
    database_path,
    artifact_store=None,
    *,
    review_base_url: str = "http://127.0.0.1:5173",
    notification_provider_keys: tuple[str, ...] = ("local",),
    resume_callback: Callable[[str, str], object] | None = None,
) -> list[str]:
    """Recover expired building operations without changing their version.

    Early phases may resume only from the durable sealed snapshot recorded for
    the submission. Once a snapshot reference exists, recovery never reads the
    live workspace again. Operations without a snapshot can still start their
    first capture under the original operation and version.
    """
    connection = connect(database_path)
    recovered: list[str] = []
    unavailable = False
    try:
        rows = connection.execute(
            "SELECT s.*,j.lease_until,j.status AS job_status FROM submission_records s "
            "LEFT JOIN build_jobs j ON j.submission_id=s.id WHERE s.state='building'"
        ).fetchall()
        for row in rows:
            submission_id = row["id"]
            if row["job_status"] == "running" and _lease_active(row["lease_until"]):
                continue
            operation = connection.execute(
                "SELECT operation_id FROM idempotency_records WHERE subject_id=? AND status='pending'",
                (submission_id,),
            ).fetchone()
            objects = connection.execute(
                "SELECT * FROM artifact_objects WHERE submission_id=? "
                "AND state IN ('reserved','stored','uncertain') ORDER BY created_at DESC",
                (submission_id,),
            ).fetchall()
            if not objects and operation is not None:
                if resume_callback is not None:
                    resume_callback(str(operation["operation_id"]), str(submission_id))
                    recovered.append(submission_id)
                continue

            worker = "recovery-" + uuid.uuid4().hex
            with transaction(connection):
                generation = claim_job(connection, submission_id, worker)
            if generation is None:
                continue
            adopted = False
            transient_failure = False
            if artifact_store is not None:
                for item in objects:
                    try:
                        if item["ref"]:
                            immutable_ref = ObjectRef(item["provider"], item["ref"])
                        else:
                            immutable_ref = artifact_store.resolve_existing(
                                item["object_id"], int(item["expected_size"]), str(item["expected_sha256"])
                            )
                        current = artifact_store.stat(immutable_ref)
                    except Exception as exc:
                        failure = _artifact_stat_failure(exc)
                        if failure == "unavailable":
                            transient_failure = True
                        if item["state"] != "reserved":
                            with transaction(connection):
                                connection.execute(
                                    "UPDATE artifact_objects SET state='uncertain',updated_at=? WHERE object_id=?",
                                    (now_iso(), item["object_id"]),
                                )
                        continue
                    reference = immutable_ref.key
                    if (
                        (current.sha256, current.size) != (item["expected_sha256"], item["expected_size"])
                        or not item["manifest_json"]
                    ):
                        with transaction(connection):
                            connection.execute(
                                "UPDATE artifact_objects SET state='uncertain',updated_at=? WHERE object_id=?",
                                (now_iso(), item["object_id"]),
                            )
                        continue
                    with transaction(connection):
                        require_job_lease(connection, submission_id, worker, generation)
                        repo = PipelineRepository(connection)
                        connection.execute(
                            "UPDATE artifact_objects SET state='stored',ref=?,updated_at=? WHERE object_id=?",
                            (reference, now_iso(), item["object_id"]),
                        )
                        repo.commit_artifact(
                            submission_id,
                            object_id=item["object_id"],
                            ref=reference,
                            sha256=item["expected_sha256"],
                            size=int(item["expected_size"]),
                            manifest=json.loads(item["manifest_json"]),
                            artifact_provider=item["provider"],
                        )
                        finish_job(connection, submission_id, worker, generation, success=True)
                        connection.execute("UPDATE storage_reservations SET state='committed',updated_at=? WHERE owner_id=? AND kind='submission' AND state='held'", (now_iso(), submission_id))
                        connection.execute(
                            "INSERT INTO submission_phases(submission_id,phase,status,updated_at) "
                            "VALUES (?,'register','succeeded',?) ON CONFLICT(submission_id,phase) "
                            "DO UPDATE SET status='succeeded',error_code=NULL,updated_at=excluded.updated_at",
                            (submission_id, now_iso()),
                        )
                        connection.execute(
                            "INSERT INTO submission_phases(submission_id,phase,status,updated_at) "
                            "VALUES (?,'notify','pending',?) ON CONFLICT(submission_id,phase) DO NOTHING",
                            (submission_id, now_iso()),
                        )
                        submitted = repo.get(submission_id)
                        enqueue_notification(
                            connection,
                            submitted,
                            state="submitted",
                            review_base_url=review_base_url,
                            channels=notification_provider_keys,
                        )
                        reconcile_notification_phase(connection, submission_id)
                    adopted = True
                    recovered.append(submission_id)
                    break
            if adopted:
                continue
            if transient_failure:
                with transaction(connection):
                    require_job_lease(connection, submission_id, worker, generation)
                    connection.execute(
                        "UPDATE build_jobs SET status='pending',lease_owner=NULL,lease_until=NULL,last_error_code='artifact_store_unavailable',heartbeat_at=? "
                        "WHERE submission_id=? AND lease_owner=? AND lease_generation=?",
                        (now_iso(), submission_id, worker, generation),
                    )
                unavailable = True
                continue

            with transaction(connection):
                require_job_lease(connection, submission_id, worker, generation)
                repo = PipelineRepository(connection)
                repo.mark_failed(submission_id, "build_interrupted")
                finish_job(
                    connection,
                    submission_id,
                    worker,
                    generation,
                    success=False,
                    error_code="build_interrupted",
                )
                connection.execute("UPDATE storage_reservations SET state='released',updated_at=? WHERE owner_id=? AND kind='submission' AND state='held'", (now_iso(), submission_id))
                connection.execute(
                    "INSERT INTO submission_phases(submission_id,phase,status,error_code,updated_at) "
                    "VALUES (?,'build','failed','build_interrupted',?) ON CONFLICT(submission_id,phase) "
                    "DO UPDATE SET status='failed',error_code='build_interrupted',updated_at=excluded.updated_at",
                    (submission_id, now_iso()),
                )
                if operation:
                    complete(
                        connection,
                        operation["operation_id"],
                        500,
                        {
                            "operation_id": operation["operation_id"],
                            "submission_id": submission_id,
                            "status": "build_failed",
                            "error": "build_interrupted",
                        },
                    )
            recovered.append(submission_id)
        if unavailable:
            raise ArtifactRecoveryUnavailable("artifact store is temporarily unavailable during recovery")
    finally:
        connection.close()
    return recovered
