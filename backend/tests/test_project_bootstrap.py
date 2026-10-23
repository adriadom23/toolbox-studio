from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.projects.validation import InvalidProjectNameError, slugify, validate_slug  # noqa: E402
from app.submissions.bootstrap import bootstrap_projects  # noqa: E402
from app.submissions.database import connect, transaction  # noqa: E402
from app.submissions.migrations import migrate  # noqa: E402
from app.submissions.repository import SubmissionRepository  # noqa: E402


def test_bootstrap_is_non_destructive_and_idempotent(tmp_path):
    frontend, backend = tmp_path / "frontend", tmp_path / "backend"
    for name in ("demo-ping", "k"):
        (frontend / name).mkdir(parents=True)
        (backend / name).mkdir(parents=True)
        (frontend / name / "project.meta.json").write_text(json.dumps({"name": name, "displayName": name}), encoding="utf-8")
    (frontend / "half").mkdir()
    path = tmp_path / "studio.sqlite3"
    migrate(path)
    connection = connect(path)
    repo = SubmissionRepository(connection)
    first = bootstrap_projects(frontend, backend, repo)
    second = bootstrap_projects(frontend, backend, repo)
    assert {item["name"] for item in first if item["status"] == "imported"} == {"demo-ping", "k"}
    assert any(item == {"name": "half", "status": "incomplete"} for item in first)
    assert all((frontend / name).is_dir() and (backend / name).is_dir() for name in ("demo-ping", "k"))
    assert connection.execute("SELECT count(*) FROM submission_events WHERE event_type = 'discovered'").fetchone()[0] == 2
    assert {item["status"] for item in second if item["name"] in {"demo-ping", "k"}} == {"existing"}
    diagnostic = connection.execute("SELECT name,status,active FROM project_bootstrap_diagnostics WHERE name='half'").fetchone()
    assert dict(diagnostic) == {"name": "half", "status": "incomplete", "active": 1}
    (backend / "half").mkdir()
    bootstrap_projects(frontend, backend, repo)
    resolved = connection.execute("SELECT active,resolved_at FROM project_bootstrap_diagnostics WHERE name='half'").fetchone()
    assert resolved["active"] == 0 and resolved["resolved_at"]
    connection.close()


def test_bootstrap_rejects_oversized_or_open_metadata_and_windows_device_slugs(tmp_path):
    frontend, backend = tmp_path / "frontend", tmp_path / "backend"
    for name in ("oversized", "open-meta"):
        (frontend / name).mkdir(parents=True)
        (backend / name).mkdir(parents=True)
    (frontend / "oversized" / "project.meta.json").write_bytes(b"{" + b" " * (64 * 1024))
    (frontend / "open-meta" / "project.meta.json").write_text('{"name":"open-meta","unexpected":true}', encoding="utf-8")
    database = tmp_path / "studio.sqlite3"
    migrate(database)
    connection = connect(database)
    diagnostics = bootstrap_projects(frontend, backend, SubmissionRepository(connection))
    assert {item["name"]: item["status"] for item in diagnostics} == {
        "open-meta": "invalid_meta",
        "oversized": "invalid_meta",
    }
    assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    connection.close()

    for value in ("con", "PRN", "aux", "NUL", "com1", "COM9", "lpt1", "LPT9"):
        with pytest.raises(InvalidProjectNameError):
            validate_slug(value.lower())
        with pytest.raises(InvalidProjectNameError):
            slugify(value)


def test_bootstrap_claims_unowned_projects_once_and_preserves_existing_owner(tmp_path):
    frontend, backend = tmp_path / "frontend", tmp_path / "backend"
    for name in ("demo", "owned"):
        (frontend / name).mkdir(parents=True)
        (backend / name).mkdir(parents=True)
    database = tmp_path / "claims.sqlite3"
    migrate(database)
    connection = connect(database)
    repository = SubmissionRepository(connection)
    with transaction(connection):
        repository.upsert_project(slug="owned", display_name="Owned", created_at=None, modified_at=None)
        connection.execute("UPDATE projects SET owner_id='alice' WHERE slug='owned'")

    bootstrap_projects(frontend, backend, repository, local_identity="local-user")
    bootstrap_projects(frontend, backend, repository, local_identity="local-user")
    owners = {row["slug"]: row["owner_id"] for row in connection.execute("SELECT slug,owner_id FROM projects")}
    assert owners == {"demo": "local-user", "owned": "alice"}
    events = connection.execute(
        "SELECT project_slug,event_type,actor_id FROM project_security_events ORDER BY project_slug,seq"
    ).fetchall()
    assert [tuple(row) for row in events] == [("demo", "project_claimed_local", "local-user")]
    connection.close()


def test_bootstrap_rolls_back_entire_batch_on_mid_discovery_failure(tmp_path, monkeypatch):
    frontend, backend = tmp_path / "frontend", tmp_path / "backend"
    for name in ("alpha", "beta"):
        (frontend / name).mkdir(parents=True)
        (backend / name).mkdir(parents=True)
    database = tmp_path / "atomic.sqlite3"
    migrate(database)
    connection = connect(database)
    repository = SubmissionRepository(connection)
    original = repository.upsert_project
    calls = 0

    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fault between discovered projects")
        return original(**kwargs)

    monkeypatch.setattr(repository, "upsert_project", fail_second)
    with pytest.raises(RuntimeError, match="fault between discovered projects"):
        bootstrap_projects(frontend, backend, repository, local_identity="local-user")
    assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM submission_events").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM project_security_events").fetchone()[0] == 0

    monkeypatch.setattr(repository, "upsert_project", original)
    bootstrap_projects(frontend, backend, repository, local_identity="local-user")
    assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM project_security_events WHERE event_type='project_claimed_local'").fetchone()[0] == 2
    connection.close()
