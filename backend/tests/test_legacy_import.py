from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import app.submissions.legacy_import as legacy_import
from app.submissions.database import connect
from app.submissions.legacy_import import _is_reparse, apply_plan, build_plan
from app.submissions.migrations import migrate
from app.submissions.repository import SubmissionRepository


def test_legacy_apply_is_idempotent_preserves_sources_and_isolates_corrupt_zip(tmp_path):
    root = tmp_path / "legacy"
    project = root / "demo"
    project.mkdir(parents=True)
    valid = project / "one.zip"
    with zipfile.ZipFile(valid, "w") as archive:
        archive.writestr("frontend/demo/App.vue", "<template>ok</template>")
        archive.writestr("MANIFEST.json", '{"format":"legacy/v1","files":[{"path":"frontend/demo/App.vue","sha256":"untrusted"}]}')
    invalid_manifest = project / "invalid-manifest.zip"
    with zipfile.ZipFile(invalid_manifest, "w") as archive:
        archive.writestr("MANIFEST.json", "[]")
    corrupt = project / "bad.zip"
    corrupt.write_bytes(b"not a zip")
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in (valid, invalid_manifest, corrupt)}
    database = tmp_path / "db.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()
    first = build_plan(root=root, database_path=database)
    result = apply_plan(first["plan_hash"], root=root, database_path=database)
    assert result == {"plan_hash": first["plan_hash"], "imported": 1, "observed": 2}
    valid_item = next(item for item in first["items"] if item["relative_path"].endswith("one.zip"))
    assert valid_item["inventory"]["declared_manifest"] == {
        "present": True, "valid_object": True, "format": "legacy/v1",
        "declared_file_count": 1, "hashes_trusted": False,
    }
    second = build_plan(root=root, database_path=database)
    replay = apply_plan(second["plan_hash"], root=root, database_path=database)
    assert replay["imported"] == 0
    connection = connect(database)
    assert connection.execute("SELECT count(*) FROM legacy_sources").fetchone()[0] == 1
    assert {row["code"] for row in connection.execute("SELECT code FROM legacy_observations")} == {
        "legacy_invalid_manifest", "legacy_invalid_zip",
    }
    connection.close()
    for path in (valid, invalid_manifest, corrupt):
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before[path.name]
        assert hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_apply_revalidates_source_after_plan_hash(tmp_path, monkeypatch):
    root = tmp_path / "legacy"
    project = root / "demo"
    project.mkdir(parents=True)
    source = project / "stable.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("frontend/demo/App.vue", "original")
    replacement = tmp_path / "replacement.zip"
    with zipfile.ZipFile(replacement, "w") as archive:
        archive.writestr("frontend/demo/App.vue", "replacement")
    original_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    replacement_sha256 = hashlib.sha256(replacement.read_bytes()).hexdigest()
    database = tmp_path / "db.sqlite3"
    migrate(database)
    connection = connect(database)
    SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
    connection.commit()
    connection.close()
    plan = build_plan(root=root, database_path=database)
    retained = tmp_path / "retained-original.zip"
    real_migrate = legacy_import.migrate

    def replace_after_plan_hash(database_path):
        source.rename(retained)
        replacement.rename(source)
        return real_migrate(database_path)

    monkeypatch.setattr(legacy_import, "migrate", replace_after_plan_hash)
    with pytest.raises(ValueError, match="legacy source changed before apply"):
        apply_plan(str(plan["plan_hash"]), root=root, database_path=database)

    assert hashlib.sha256(retained.read_bytes()).hexdigest() == original_sha256
    assert hashlib.sha256(source.read_bytes()).hexdigest() == replacement_sha256
    connection = connect(database)
    assert connection.execute("SELECT COUNT(*) FROM legacy_sources").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM submission_records").fetchone()[0] == 0
    connection.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows descriptor and junction regression")
def test_windows_legacy_inspection_retains_file_handle_and_rejects_junction(tmp_path, monkeypatch):
    root = tmp_path / "legacy"
    project = root / "demo"
    project.mkdir(parents=True)
    archive_path = project / "stable.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("frontend/demo/App.vue", "safe")
    retained = tmp_path / "retained.zip"
    rename_errors = []
    original_inspect = legacy_import._inspect_zip

    def inspect_while_replacement_is_attempted(handle):
        def replace_path():
            try:
                archive_path.rename(retained)
            except OSError as exc:
                rename_errors.append(exc)

        worker = threading.Thread(target=replace_path)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        return original_inspect(handle)

    monkeypatch.setattr(legacy_import, "_inspect_zip", inspect_while_replacement_is_attempted)
    plan = build_plan(root=root, database_path=tmp_path / "missing.sqlite3")
    item = plan["items"][0]
    assert item["status"] == "ready"
    assert len(item["file_id"].split(":")) == 3
    assert rename_errors and archive_path.is_file() and not retained.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.zip").write_bytes(archive_path.read_bytes())
    junction = root / "linked"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation unavailable: {created.stderr or created.stdout}")
    next_plan = build_plan(root=root, database_path=tmp_path / "missing.sqlite3")
    assert {"relative_path": "linked", "code": "legacy_reparse_rejected"} in next_plan["diagnostics"]


def test_windows_reparse_attribute_is_rejected_fail_closed():
    assert _is_reparse(SimpleNamespace(st_file_attributes=0x400))
    assert not _is_reparse(SimpleNamespace(st_file_attributes=0))
