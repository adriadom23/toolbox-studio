from __future__ import annotations

from datetime import datetime, timedelta, timezone


class LeaseLost(RuntimeError):
    code = "build_lease_lost"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def claim_job(connection, submission_id: str, worker_id: str, lease_seconds: int = 60) -> int | None:
    now = _now()
    row = connection.execute("SELECT lease_generation,lease_until,status FROM build_jobs WHERE submission_id=?", (submission_id,)).fetchone()
    if not row or row["status"] in {"completed", "failed"}:
        return None
    lease_until = datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None
    if lease_until and lease_until > now and row["status"] == "running":
        return None
    generation = int(row["lease_generation"]) + 1
    cursor = connection.execute(
        "UPDATE build_jobs SET status='running',attempt=attempt+1,lease_owner=?,lease_generation=?,lease_until=?,heartbeat_at=? WHERE submission_id=? AND lease_generation=?",
        (worker_id, generation, (now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), submission_id, row["lease_generation"]),
    )
    return generation if cursor.rowcount == 1 else None


def heartbeat_job(connection, submission_id: str, worker_id: str, generation: int, lease_seconds: int = 60) -> bool:
    now = _now()
    cursor = connection.execute(
        "UPDATE build_jobs SET lease_until=?,heartbeat_at=? WHERE submission_id=? AND status='running' AND lease_owner=? AND lease_generation=?",
        ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), submission_id, worker_id, generation),
    )
    return cursor.rowcount == 1


def require_job_lease(connection, submission_id: str, worker_id: str, generation: int) -> None:
    row = connection.execute(
        "SELECT lease_until FROM build_jobs WHERE submission_id=? AND status='running' AND lease_owner=? AND lease_generation=?",
        (submission_id, worker_id, generation),
    ).fetchone()
    if row is None or not row["lease_until"] or datetime.fromisoformat(row["lease_until"]) <= _now():
        raise LeaseLost("build lease is no longer owned")


def finish_job(connection, submission_id: str, worker_id: str, generation: int, *, success: bool, error_code: str | None = None) -> bool:
    require_job_lease(connection, submission_id, worker_id, generation)
    cursor = connection.execute(
        "UPDATE build_jobs SET status=?,last_error_code=?,heartbeat_at=?,lease_until=NULL WHERE submission_id=? AND lease_owner=? AND lease_generation=?",
        ("completed" if success else "failed", error_code, _now().isoformat(), submission_id, worker_id, generation),
    )
    return cursor.rowcount == 1
