from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.submissions.database import connect
from app.submissions.doctor import apply_logical_plan, storage_plan
from app.submissions.migrations import migrate
from app.submissions.providers import ObjectStat
from app.submissions.repository_v2 import now_iso


def test_storage_doctor_dry_run_and_apply_are_logical_only(tmp_path):
    database = tmp_path / "db.sqlite3"
    runtime = tmp_path / "runtime"
    migrate(database)
    connection = connect(database)
    now = now_iso()
    connection.execute("INSERT INTO artifact_objects(object_id,ref,expected_sha256,expected_size,state,provider,created_at,updated_at) VALUES ('object','missing','bad',1,'committed','filesystem',?,?)", (now, now))
    connection.execute("INSERT INTO storage_reservations(id,kind,owner_id,bytes,state,expires_at,updated_at) VALUES ('reservation','staging','owner',10,'held',?,?)", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), now))
    connection.commit()
    connection.close()
    plan = storage_plan(database, runtime)
    assert len(plan["actions"]) == 2 and plan["physical_deletions"] == []
    connection = connect(database)
    assert connection.execute("SELECT state FROM artifact_objects").fetchone()[0] == "committed"
    connection.close()
    result = apply_logical_plan(database, runtime, plan["plan_hash"])
    assert result["physical_deletions"] == []
    connection = connect(database)
    assert connection.execute("SELECT state FROM artifact_objects").fetchone()[0] == "quarantined"
    assert connection.execute("SELECT state FROM storage_reservations").fetchone()[0] == "released"
    connection.close()


def test_storage_doctor_uses_registered_s3_provider_without_false_quarantine(tmp_path):
    database = tmp_path / "s3.sqlite3"
    runtime = tmp_path / "runtime"
    migrate(database)
    connection = connect(database)
    now = now_iso()
    connection.execute(
        "INSERT INTO artifact_objects(object_id,ref,expected_sha256,expected_size,state,provider,created_at,updated_at) "
        "VALUES ('s3-object','s3v1:immutable','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',3,'committed','s3',?,?)",
        (now, now),
    )
    connection.commit()
    connection.close()

    class ValidS3Store:
        def stat(self, ref):
            assert ref.provider == "s3" and ref.key == "s3v1:immutable"
            return ObjectStat(3, "a" * 64)

    providers = {"s3": ValidS3Store()}
    plan = storage_plan(database, runtime, providers)
    assert plan["actions"] == [] and plan["diagnostics"] == []
    result = apply_logical_plan(database, runtime, plan["plan_hash"], providers)
    assert result["applied"] == 0
    connection = connect(database)
    assert connection.execute("SELECT state FROM artifact_objects WHERE object_id='s3-object'").fetchone()[0] == "committed"
    connection.close()


def test_storage_doctor_fails_closed_for_unavailable_provider(tmp_path):
    database = tmp_path / "unsupported.sqlite3"
    migrate(database)
    connection = connect(database)
    now = now_iso()
    connection.execute(
        "INSERT INTO artifact_objects(object_id,ref,expected_sha256,expected_size,state,provider,created_at,updated_at) "
        "VALUES ('remote-object','remote-ref',?,1,'committed','s3',?,?)",
        ("b" * 64, now, now),
    )
    connection.commit()
    connection.close()
    plan = storage_plan(database, tmp_path / "runtime")
    assert plan["actions"] == []
    assert plan["diagnostics"] == [{"code": "provider_unsupported", "provider": "s3", "object_id": "remote-object"}]
