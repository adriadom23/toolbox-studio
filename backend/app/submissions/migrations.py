from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.submissions.database import connect

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class MigrationError(RuntimeError):
    pass


# Migration 0012 predates the additive-only validator. SQLite cannot replace a
# trigger in place, so this one checksum-bound DROP is retained solely to relax
# expired replay-nonce retention without changing already-published migration
# bytes. No other DROP is accepted.
_LEGACY_ADDITIVE_EXCEPTIONS = {
    (12, "0012_concurrency_retry_retention.sql", "DROP TRIGGER IF EXISTS proxy_replay_nonces_no_delete;"),
}


def _migration_statements(sql: str, migration_name: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for character in sql:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    if pending.strip():
        raise MigrationError(f"incomplete migration statement: {migration_name}")
    return statements


def _validate_migration_statement(version: int, migration_name: str, statement: str) -> None:
    if (version, migration_name, statement) in _LEGACY_ADDITIVE_EXCEPTIONS:
        return
    normalized = re.sub(r"\s+", " ", statement).strip()
    if re.match(r"^CREATE TABLE (?:IF NOT EXISTS )?[A-Za-z_][A-Za-z0-9_]*\s*\(", normalized, re.IGNORECASE):
        return
    if re.match(
        r"^CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?[A-Za-z_][A-Za-z0-9_]* ON [A-Za-z_][A-Za-z0-9_]*\s*\(",
        normalized,
        re.IGNORECASE,
    ):
        return
    if re.match(r"^CREATE TRIGGER (?:IF NOT EXISTS )?[A-Za-z_][A-Za-z0-9_]*\s+", normalized, re.IGNORECASE):
        parts = re.split(r"\bBEGIN\b", normalized, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2 or re.search(
            r"\b(?:ALTER|ATTACH|DELETE|DETACH|DROP|INSERT|REPLACE|UPDATE|VACUUM)\b",
            parts[1],
            re.IGNORECASE,
        ):
            raise MigrationError(f"migration statement is not additive: {migration_name}")
        return
    if re.match(
        r"^ALTER TABLE [A-Za-z_][A-Za-z0-9_]* ADD COLUMN [A-Za-z_][A-Za-z0-9_]*\s+",
        normalized,
        re.IGNORECASE,
    ):
        return
    raise MigrationError(f"migration statement is not additive: {migration_name}")


def _validated_migration_statements(version: int, migration_path: Path, sql: str) -> list[str]:
    statements = _migration_statements(sql, migration_path.name)
    for statement in statements:
        _validate_migration_statement(version, migration_path.name, statement)
    return statements


def _migration_files() -> list[tuple[int, Path, str]]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(path.name.split("_", 1)[0])
        migrations.append((version, path, path.read_text(encoding="utf-8")))
    return migrations


def _integrity_ok(connection: sqlite3.Connection) -> bool:
    return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@contextmanager
def _migration_lock(db_path: Path) -> Iterator[None]:
    lock_path = db_path.with_name(f"{db_path.name}.migration.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _verified_backup(connection: sqlite3.Connection, db_path: Path) -> Path:
    database_size = db_path.stat().st_size
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    logical_size = page_count * page_size
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0
    backup_size = max(logical_size, database_size + wal_size)
    safety_margin = max(16 * 1024 * 1024, backup_size // 10)
    try:
        free_bytes = shutil.disk_usage(db_path.parent).free
    except OSError as exc:
        raise MigrationError("migration backup capacity check failed") from exc
    if free_bytes < backup_size + safety_margin:
        raise MigrationError("insufficient storage for migration backup")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{stamp}")
    destination = sqlite3.connect(backup_path)
    try:
        connection.backup(destination)
        if not _integrity_ok(destination):
            raise MigrationError("backup integrity check failed")
    finally:
        destination.close()
    return backup_path


def _reconcile_preview_duplicates_before_v12(connection: sqlite3.Connection) -> None:
    groups = connection.execute(
        "SELECT subject_id,input_sha256,harness_sha256 FROM preview_build_operations "
        "WHERE subject_kind='submission' AND state IN ('queued','running') "
        "GROUP BY subject_id,input_sha256,harness_sha256 HAVING COUNT(*)>1"
    ).fetchall()
    for group in groups:
        operations = connection.execute(
            "SELECT * FROM preview_build_operations WHERE subject_kind='submission' AND subject_id=? "
            "AND input_sha256 IS ? AND harness_sha256 IS ? AND state IN ('queued','running') "
            "ORDER BY created_at,operation_id",
            (group["subject_id"], group["input_sha256"], group["harness_sha256"]),
        ).fetchall()
        canonical = str(operations[0]["operation_id"])
        for duplicate in operations[1:]:
            operation_id = str(duplicate["operation_id"])
            connection.execute(
                "UPDATE preview_build_operations SET state='failed',phase='failed',error_code='duplicate_preview_reconciled',"
                "recoverable=1,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE operation_id=?",
                (datetime.now(timezone.utc).isoformat(), operation_id),
            )
            connection.execute(
                "UPDATE storage_reservations SET state='released',updated_at=? WHERE id=? AND state='held'",
                (datetime.now(timezone.utc).isoformat(), duplicate["reservation_id"]),
            )
            submission_id = str(duplicate["subject_id"])
            next_seq = int(connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM submission_operation_events WHERE submission_id=?",
                (submission_id,),
            ).fetchone()[0])
            connection.execute(
                "INSERT INTO submission_operation_events(submission_id,seq,event_type,actor_id,data_json,created_at,"
                "actor_roles_json,old_state,new_state,correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    submission_id,
                    next_seq,
                    "preview_retry_reconciled",
                    "system",
                    json.dumps({"canonical_operation_id": canonical, "duplicate_operation_id": operation_id}, sort_keys=True, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(),
                    "[]",
                    str(duplicate["state"]),
                    "failed",
                    duplicate["correlation_id"],
                ),
            )


def _recorded_migrations(connection: sqlite3.Connection) -> dict[int, str]:
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    if not exists:
        return {}
    return {int(row["version"]): str(row["checksum"]) for row in connection.execute("SELECT version,checksum FROM schema_migrations").fetchall()}


def migrate(db_path: Path | str) -> list[int]:
    path = Path(db_path)
    with _migration_lock(path):
        existed = path.exists() and path.stat().st_size > 0
        connection = connect(path)
        applied_now: list[int] = []
        try:
            if existed and not _integrity_ok(connection):
                raise MigrationError("database integrity check failed")
            migrations = _migration_files()
            supported = migrations[-1][0] if migrations else 0
            recorded = _recorded_migrations(connection)
            if recorded and max(recorded) > supported:
                raise MigrationError("database schema is newer than this application")
            pending: list[tuple[int, Path, str, str]] = []
            for version, migration_path, sql in migrations:
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if version in recorded:
                    if recorded[version] != checksum:
                        raise MigrationError(f"migration checksum mismatch: {migration_path.name}")
                else:
                    pending.append((version, migration_path, sql, checksum))
            if not pending:
                return []
            validated_pending = [
                (version, migration_path, _validated_migration_statements(version, migration_path, sql), checksum)
                for version, migration_path, sql, checksum in pending
            ]
            if existed:
                _verified_backup(connection, path)
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for version, migration_path, statements, checksum in validated_pending:
                if version == 12:
                    _reconcile_preview_duplicates_before_v12(connection)
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version,checksum,applied_at) VALUES (?,?,?)",
                    (version, checksum, datetime.now(timezone.utc).isoformat()),
                )
                applied_now.append(version)
            if not _integrity_ok(connection):
                raise MigrationError("post-migration integrity check failed")
            connection.commit()
            return applied_now
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
