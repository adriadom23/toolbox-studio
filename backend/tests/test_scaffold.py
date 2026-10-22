"""Unit tests for ``app.projects.scaffold`` (design.md §6.2/§6.3/§6.4)."""
from __future__ import annotations

import ast
import sys
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
