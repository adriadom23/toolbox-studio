"""Unit tests for ``app.projects.scaffold`` (design.md §6.2/§6.3/§6.4)."""
from __future__ import annotations

import ast
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.projects import registry, scaffold  # noqa: E402


@pytest.fixture()
def isolated_projects_dirs(tmp_path, monkeypatch):
    """Point the registry/scaffold module-level dirs at a temp tree so this
    test never touches the real frontend/backend projects folders."""

    fe_dir = tmp_path / "frontend" / "src" / "projects"
    be_dir = tmp_path / "backend" / "app" / "projects"
    fe_dir.mkdir(parents=True)
    be_dir.mkdir(parents=True)
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", fe_dir)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", be_dir)
    monkeypatch.setattr(scaffold, "FRONTEND_PROJECTS_DIR", fe_dir)
    monkeypatch.setattr(scaffold, "BACKEND_PROJECTS_DIR", be_dir)
    return fe_dir, be_dir


def test_create_project_valid_name_produces_slug_and_four_files(isolated_projects_dirs):
    fe_dir, be_dir = isolated_projects_dirs

    meta = scaffold.create_project("Dock Health Monitor")

    assert meta["name"] == "dock-health-monitor"
    assert meta["displayName"] == "Dock Health Monitor"
    assert meta["status"] == "draft"
    assert meta["created_at"] == meta["modified_at"]

    fe_project = fe_dir / "dock-health-monitor"
    be_project = be_dir / "dock-health-monitor"

    vue_file = fe_project / "DockHealthMonitor.vue"
    meta_file = fe_project / "project.meta.json"
    init_file = be_project / "__init__.py"
    routes_file = be_project / "routes.py"

    assert vue_file.exists()
    assert meta_file.exists()
    assert init_file.exists()
    assert routes_file.exists()

    vue_content = vue_file.read_text(encoding="utf-8")
    assert "<template>" in vue_content
    assert "<script setup>" in vue_content
    assert "DockHealthMonitor" in vue_content
    assert "/projects/dock-health-monitor/app/ping" in vue_content
    # The corrected template (design.md §6.3): no probeAll() CALL anywhere,
    # and no `onMounted` IMPORT from vue (a doc comment explaining why
    # probeAll()/onMounted aren't needed is expected — it matches the
    # design's own corrected template verbatim — but it is prose, not a
    # call site or an import statement).
    assert "identity.probeAll()" not in vue_content
    script_block = vue_content.split('<script setup>', 1)[1].split("</script>", 1)[0]
    import_lines = [line for line in script_block.splitlines() if line.strip().startswith("import ")]
    assert not any("onMounted" in line for line in import_lines)
    assert 'import { ref } from "vue"' in vue_content
    assert ':identity="identity"' in vue_content

    routes_content = routes_file.read_text(encoding="utf-8")
    # Valid Python via ast.parse.
    ast.parse(routes_content)
    assert "blueprint = Blueprint(" in routes_content
    assert '"status": "ok", "project": "dock-health-monitor"' in routes_content


def test_create_project_duplicate_name_is_rejected(isolated_projects_dirs):
    scaffold.create_project("Dock Health Monitor")

    with pytest.raises(scaffold.ProjectAlreadyExistsError):
        scaffold.create_project("Dock Health Monitor")

    with pytest.raises(scaffold.ProjectAlreadyExistsError):
        scaffold.create_project("dock-health-monitor")


@pytest.mark.parametrize("raw_name", ["123abc", "", "   ", "---", "9", "42-widgets"])
def test_create_project_invalid_names_are_rejected(isolated_projects_dirs, raw_name):
    with pytest.raises(scaffold.InvalidProjectNameError):
        scaffold.create_project(raw_name)


def test_leading_hyphens_in_raw_name_are_trimmed_to_a_valid_slug(isolated_projects_dirs):
    # "-leading-hyphen" sanitizes to "leading-hyphen" (leading/trailing "-"
    # trimmed per design.md §6.2) — this is valid sanitization, not a
    # rejected name.
    meta = scaffold.create_project("-leading-hyphen")
    assert meta["name"] == "leading-hyphen"


def test_failed_scaffold_is_quarantined_without_deletion(isolated_projects_dirs, monkeypatch):
    fe_dir, be_dir = isolated_projects_dirs

    def fail_template(_slug):
        raise RuntimeError("template failed")

    monkeypatch.setattr(scaffold, "_routes_py_template", fail_template)
    with pytest.raises(RuntimeError, match="template failed"):
        scaffold.create_project("Broken Tool")

    assert not (fe_dir / "broken-tool").exists()
    assert not (be_dir / "broken-tool").exists()
    assert list(fe_dir.glob(".broken-tool-fe-*.quarantine-*"))
    assert list(be_dir.glob(".broken-tool-be-*.quarantine-*"))
    assert (be_dir / ".broken-tool.scaffold-journal.json").is_file()


def test_concurrent_creation_never_quarantines_the_winner(isolated_projects_dirs):
    fe_dir, be_dir = isolated_projects_dirs

    def create():
        try:
            return scaffold.create_project("Concurrent Tool")["name"]
        except scaffold.ProjectAlreadyExistsError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create(), range(2)))
    assert sorted(results) == ["concurrent-tool", "conflict"]
    assert (fe_dir / "concurrent-tool" / "ConcurrentTool.vue").is_file()
    assert (be_dir / "concurrent-tool" / "routes.py").is_file()


@pytest.mark.parametrize("corruption", ["staging-traversal", "target-traversal", "duplicate-root", "durable-mismatch"])
def test_creation_recovery_rejects_unconfined_or_durable_mismatched_items(isolated_projects_dirs, tmp_path, corruption):
    fe_dir, be_dir = isolated_projects_dirs
    operation_id = "op_" + "a" * 32
    journal_root = tmp_path / "journals"
    journal_root.mkdir()
    fe_staging = fe_dir / ".safe-tool-fe-staging"
    be_staging = be_dir / ".safe-tool-be-staging"
    fe_staging.mkdir()
    be_staging.mkdir()
    (fe_staging / "SafeTool.vue").write_text("safe", encoding="utf-8")
    (be_staging / "routes.py").write_text("safe", encoding="utf-8")
    manifest = {
        "version": 1,
        "operation_id": operation_id,
        "slug": "safe-tool",
        "status": "prepared",
        "meta": {"name": "safe-tool", "displayName": "Safe Tool", "status": "draft"},
        "items": [
            {
                "root": "frontend", "staging_name": fe_staging.name, "target_name": "safe-tool",
                "sha256": scaffold._tree_digest(fe_staging), "status": "prepared",
            },
            {
                "root": "backend", "staging_name": be_staging.name, "target_name": "safe-tool",
                "sha256": scaffold._tree_digest(be_staging), "status": "prepared",
            },
        ],
    }
    durable = [
        {
            "item_kind": item["root"], "staging_name": item["staging_name"],
            "target_name": item["target_name"], "expected_sha256": item["sha256"], "status": "prepared",
        }
        for item in manifest["items"]
    ]
    if corruption == "staging-traversal":
        manifest["items"][0]["staging_name"] = "../outside"
    elif corruption == "target-traversal":
        manifest["items"][0]["target_name"] = "../outside"
    elif corruption == "duplicate-root":
        manifest["items"][1]["root"] = "frontend"
    else:
        durable[0]["expected_sha256"] = "f" * 64
    (journal_root / f"{operation_id}.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ValueError):
        scaffold.recover_project_creation(operation_id, journal_root, durable)

    assert fe_staging.is_dir() and be_staging.is_dir()
    assert not (fe_dir / "safe-tool").exists() and not (be_dir / "safe-tool").exists()
    assert marker.read_text(encoding="utf-8") == "unchanged"
