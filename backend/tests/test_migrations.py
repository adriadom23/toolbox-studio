from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import app.submissions.migrations as migrations_module  # noqa: E402
from app.submissions.database import connect, transaction  # noqa: E402
from app.submissions.migrations import MigrationError, _verified_backup, migrate  # noqa: E402
from app.submissions.repository import SubmissionRepository  # noqa: E402
from app.submissions.repository_v2 import PipelineRepository, now_iso  # noqa: E402


def test_migration_is_idempotent_and_backup_only_precedes_pending_ddl(tmp_path):
    path = tmp_path / "studio.sqlite3"
    assert migrate(path) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    assert migrate(path) == []
    assert list(tmp_path.glob("studio.sqlite3.backup-*")) == []
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 18
    connection.close()

    legacy = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.commit()
    connection.close()
    assert migrate(legacy) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    backups = list(tmp_path.glob("legacy.sqlite3.backup-*"))
    assert len(backups) == 1
    assert migrate(legacy) == []
    assert list(tmp_path.glob("legacy.sqlite3.backup-*")) == backups


def test_backup_capacity_is_checked_before_file_creation_or_ddl(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        "app.submissions.migrations.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )
    with pytest.raises(MigrationError, match="insufficient storage"):
        migrate(path)
    assert list(tmp_path.glob("legacy.sqlite3.backup-*")) == []
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone() is None
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='legacy'").fetchone()[0] == "legacy"
    connection.close()


def test_backup_preflight_accounts_for_pending_wal_bytes(tmp_path, monkeypatch):
    path = tmp_path / "wal.sqlite3"
    connection = connect(path)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE payload(value BLOB)")
    connection.executemany("INSERT INTO payload(value) VALUES (randomblob(65536))", [()] * 64)
    connection.commit()
    wal_path = path.with_name(f"{path.name}-wal")
    assert wal_path.stat().st_size > path.stat().st_size
    old_estimate = path.stat().st_size + 16 * 1024 * 1024
    monkeypatch.setattr(
        "app.submissions.migrations.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": old_estimate})(),
    )
    with pytest.raises(MigrationError, match="insufficient storage"):
        _verified_backup(connection, path)
    connection.close()
    assert list(tmp_path.glob("wal.sqlite3.backup-*")) == []


def test_upgrade_v11_reconciles_duplicate_active_submission_previews(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.sqlite3"
    all_migrations = migrations_module._migration_files()
    monkeypatch.setattr(migrations_module, "_migration_files", lambda: [item for item in all_migrations if item[0] <= 11])
    assert migrate(path) == list(range(1, 12))
    monkeypatch.setattr(migrations_module, "_migration_files", lambda: all_migrations)

    submission_id = "sub_" + "a" * 32
    connection = connect(path)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(
            submission_id=submission_id,
            slug="demo",
            version=1,
            actor="alice",
            title="Demo",
            description="Upgrade",
            correlation_id="upgrade-test",
        )
        now = now_iso()
        for index in range(2):
            reservation_id = f"res_{index}"
            operation_id = f"pop_{index}"
            connection.execute(
                "INSERT INTO storage_reservations(id,kind,owner_id,bytes,state,expires_at,updated_at) "
                "VALUES (?,?,?,1,'held',?,?)",
                (reservation_id, "preview", operation_id, now, now),
            )
            connection.execute(
                "INSERT INTO preview_build_operations(operation_id,actor_id,project_slug,idempotency_key,request_sha256,state,phase,"
                "reservation_id,created_at,updated_at,subject_kind,subject_id,input_sha256,harness_sha256) "
                "VALUES (?,?,?,?,?,'queued','reserved',?,?,?,?,?,?,?)",
                (
                    operation_id,
                    f"actor-{index}",
                    "demo",
                    f"upgrade-key-{index}",
                    f"request-{index}",
                    reservation_id,
                    now,
                    now,
                    "submission",
                    submission_id,
                    "input",
                    "harness",
                ),
            )
    connection.close()

    assert migrate(path) == [12, 13, 14, 15, 16, 17, 18]
    connection = connect(path)
    operations = connection.execute(
        "SELECT operation_id,state,error_code FROM preview_build_operations ORDER BY operation_id"
    ).fetchall()
    assert [row["state"] for row in operations].count("queued") == 1
    duplicate = next(row for row in operations if row["state"] == "failed")
    assert duplicate["error_code"] == "duplicate_preview_reconciled"
    assert connection.execute("SELECT COUNT(*) FROM storage_reservations WHERE state='released'").fetchone()[0] == 1
    event = connection.execute(
        "SELECT event_type,actor_id,data_json FROM submission_operation_events WHERE submission_id=? ORDER BY seq DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    assert event["event_type"] == "preview_retry_reconciled" and event["actor_id"] == "system"
    assert duplicate["operation_id"] in event["data_json"]
    connection.close()


def test_only_exact_v12_legacy_drop_exception_is_allowed():
    exact = "DROP TRIGGER IF EXISTS proxy_replay_nonces_no_delete;"
    migrations_module._validate_migration_statement(12, "0012_concurrency_retry_retention.sql", exact)
    deviations = (
        (11, "0012_concurrency_retry_retention.sql", exact),
        (12, "0012_concurrency_retry_retention_v2.sql", exact),
        (12, "0012_concurrency_retry_retention.sql", "drop trigger if exists proxy_replay_nonces_no_delete;"),
        (12, "0012_concurrency_retry_retention.sql", "DROP  TRIGGER IF EXISTS proxy_replay_nonces_no_delete;"),
        (12, "0012_concurrency_retry_retention.sql", "DROP TRIGGER IF EXISTS proxy_replay_nonces_no_update;"),
    )
    for version, name, statement in deviations:
        with pytest.raises(MigrationError, match="not additive"):
            migrations_module._validate_migration_statement(version, name, statement)


@pytest.mark.parametrize(
    "malicious_sql",
    (
        "CREATE TABLE safe(value TEXT);\nDROP TABLE safe;",
        "CREATE TABLE safe(value TEXT);\nDELETE FROM safe;",
        "CREATE TABLE safe(value TEXT);\nALTER TABLE safe RENAME TO rebuilt;",
        "CREATE TABLE rebuilt AS SELECT 1 AS value;",
        "CREATE TRIGGER unsafe AFTER INSERT ON safe BEGIN DELETE FROM safe; END;",
    ),
)
def test_migration_allowlist_rejects_non_additive_sql_before_ddl(tmp_path, monkeypatch, malicious_sql):
    path = tmp_path / "blocked.sqlite3"
    migration_path = tmp_path / "0099_blocked.sql"
    monkeypatch.setattr(
        migrations_module,
        "_migration_files",
        lambda: [(99, migration_path, malicious_sql)],
    )
    with pytest.raises(MigrationError, match="not additive"):
        migrate(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name IN ('safe','rebuilt','schema_migrations')").fetchall() == []
    connection.close()


def test_future_schema_is_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    migrate(path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO schema_migrations VALUES (99, 'x', 'now')")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="newer"):
        migrate(path)


def test_corrupt_existing_database_is_rejected(tmp_path):
    path = tmp_path / "bad.sqlite3"
    path.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        migrate(path)
