from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.projects import registry
from app.submissions.snapshot import SafeSnapshotBuilder


def test_backend_runner_emits_closed_protocol(tmp_path):
    snapshot = SafeSnapshotBuilder(registry.FRONTEND_PROJECTS_DIR, registry.BACKEND_PROJECTS_DIR).build("demo-ping", tmp_path / "snapshot")
    runner = BACKEND / "app" / "submissions" / "runners" / "validate_submission_project.py"
    result = subprocess.run([sys.executable, "-I", "-B", str(runner), str(snapshot.root), "demo-ping"], capture_output=True, text=True, timeout=30, check=False)
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload == {"version": 1, "ok": True, "checks": [{"code": "blueprint_ping", "status": "passed"}]}


def test_backend_runner_rejects_python_failure(tmp_path):
    snapshot = SafeSnapshotBuilder(registry.FRONTEND_PROJECTS_DIR, registry.BACKEND_PROJECTS_DIR).build("demo-ping", tmp_path / "snapshot")
    routes = snapshot.root / "backend" / "app" / "projects" / "demo-ping" / "routes.py"
    routes.write_text("this is invalid python !!!", encoding="utf-8")
    runner = BACKEND / "app" / "submissions" / "runners" / "validate_submission_project.py"
    result = subprocess.run([sys.executable, "-I", "-B", str(runner), str(snapshot.root), "demo-ping"], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)["checks"][0]["code"] == "import_failed"
