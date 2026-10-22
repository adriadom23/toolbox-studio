"""Unit tests for ``app.projects.packaging`` (design.md §4.4/§7).

NEVER configures a real Slack webhook or real SMTP/SES credentials: the
Slack path below uses a deliberately invalid webhook URL
(``https://hooks.slack.com/services/TEST/TEST/TEST``, per this
workspace's standing no-real-Slack-sends rule) and email stays disabled.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import owner_config  # noqa: E402
from app.projects import packaging, registry, scaffold  # noqa: E402


@pytest.fixture()
def isolated_project(tmp_path, monkeypatch):
    fe_dir = tmp_path / "frontend" / "src" / "projects"
    be_dir = tmp_path / "backend" / "app" / "projects"
    fe_dir.mkdir(parents=True)
    be_dir.mkdir(parents=True)
    monkeypatch.setattr(registry, "FRONTEND_PROJECTS_DIR", fe_dir)
    monkeypatch.setattr(registry, "BACKEND_PROJECTS_DIR", be_dir)
    monkeypatch.setattr(scaffold, "FRONTEND_PROJECTS_DIR", fe_dir)
    monkeypatch.setattr(scaffold, "BACKEND_PROJECTS_DIR", be_dir)
    monkeypatch.setattr(packaging, "FRONTEND_PROJECTS_DIR", fe_dir)
    monkeypatch.setattr(packaging, "BACKEND_PROJECTS_DIR", be_dir)
    submissions_dir = tmp_path / "submissions"
    monkeypatch.setattr(packaging, "SUBMISSIONS_DIR", submissions_dir)

    scaffold.create_project("Demo Project")
    return "demo-project"


def test_build_package_produces_correct_zip_and_manifest(isolated_project):
    zip_path = packaging.build_package(isolated_project)

    assert zip_path.exists()
    assert zip_path.suffix == ".zip"

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert f"frontend/{isolated_project}/DemoProject.vue" in names
        assert f"frontend/{isolated_project}/project.meta.json" in names
        assert f"backend/{isolated_project}/__init__.py" in names
        assert f"backend/{isolated_project}/routes.py" in names
        assert "MANIFEST.json" in names

        manifest = json.loads(zf.read("MANIFEST.json"))
        assert manifest["project"] == isolated_project
        assert manifest["frontend_root"] == f"frontend/src/projects/{isolated_project}/"
        assert manifest["backend_root"] == f"backend/app/projects/{isolated_project}/"
        assert "packaged_at" in manifest


def test_build_package_excludes_pycache(isolated_project):
    """A project's backend folder accumulates `__pycache__/*.pyc` the
    moment `register_all_project_blueprints` imports its routes.py
    (app/projects/registry.py) — host-specific compiled bytecode that a
    submitted package must not ship (it is not source the owner needs to
    review, and matches the Studio repo's own `.gitignore` policy)."""

    be_dir = packaging.BACKEND_PROJECTS_DIR / isolated_project
    pycache_dir = be_dir / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "routes.cpython-313.pyc").write_bytes(b"\x00\x01")

    zip_path = packaging.build_package(isolated_project)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert not any("__pycache__" in name for name in names)
        assert f"backend/{isolated_project}/routes.py" in names


def test_build_package_missing_project_raises():
    with pytest.raises(packaging.ProjectNotFoundError):
        packaging.build_package("does-not-exist")


def test_notify_owners_invalid_webhook_and_disabled_email_never_raises(
    isolated_project, monkeypatch
):
    config_path = Path(packaging.__file__).resolve()  # unused, just anchors import order
    del config_path

    disabled_config = {
        "owners": ["adriadom", "zesparzj"],
        "notifications": {
            "email": {"enabled": False, "to": [], "transport": "smtp"},
            "slack": {
                "enabled": True,
                # Deliberately invalid webhook, per this workspace's standing
                # no-real-Slack-sends rule — never a real webhook.
                "webhook_url": "https://hooks.slack.com/services/TEST/TEST/TEST",
            },
        },
    }
    monkeypatch.setattr(owner_config, "load_owner_config", lambda: disabled_config)

    zip_path = packaging.build_package(isolated_project)

    results = packaging.notify_owners(isolated_project, zip_path, submitted_by="tester")

    assert results == {"email": False, "slack": False}
