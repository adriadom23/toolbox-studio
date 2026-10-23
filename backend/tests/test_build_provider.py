from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.projects import registry
from app.submissions.build import BuildFailed, LocalSubprocessBuildProvider, _run, sanitized_environment
from app.submissions.providers import BuildOutputs, BuildRequest
from app.submissions.snapshot import SafeSnapshotBuilder


def test_demo_ping_runs_real_vite_and_backend_runner(tmp_path):
    snapshot = SafeSnapshotBuilder(registry.FRONTEND_PROJECTS_DIR, registry.BACKEND_PROJECTS_DIR).build("demo-ping", tmp_path / "snapshot")
    frontend_root = registry.FRONTEND_PROJECTS_DIR.parents[1]
    result = LocalSubprocessBuildProvider(frontend_root).execute(BuildRequest("demo-ping", snapshot.root, tmp_path / "output", 120))
    assert result.ok
    assert (tmp_path / "output" / "preview" / "index.html").is_file()


def test_build_environment_drops_credentials(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canary")
    monkeypatch.setenv("HTTPS_PROXY", "canary")
    environment = sanitized_environment()
    assert "AWS_SECRET_ACCESS_KEY" not in environment and "HTTPS_PROXY" not in environment


def test_subprocess_timeout_is_a_build_failure(tmp_path):
    with pytest.raises(BuildFailed) as error:
        _run([sys.executable, "-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout=1, environment=sanitized_environment(), max_output=1024)
    assert error.value.code == "build_timeout"


def test_subprocess_output_is_drained_with_a_hard_limit(tmp_path):
    result = _run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000); sys.stderr.write('y' * 200000)"],
        cwd=tmp_path, timeout=10, environment=sanitized_environment(), max_output=1024,
    )
    assert len(result.stdout.encode()) == 1024
    assert len(result.stderr.encode()) == 1024
    assert result.stdout_truncated and result.stderr_truncated


def test_local_provider_uses_configured_global_and_per_project_capacity(tmp_path):
    provider = LocalSubprocessBuildProvider(
        tmp_path,
        max_builds_global=4,
        max_builds_per_project=2,
        capacity_wait_seconds=1,
    )
    entered = threading.Barrier(3)
    release = threading.Event()

    def execute_locked(_request):
        entered.wait(timeout=2)
        assert release.wait(2)
        return BuildOutputs(True, "harness", ())

    provider._execute_locked = execute_locked
    request = BuildRequest("demo", tmp_path / "snapshot", tmp_path / "output", 10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        builds = [pool.submit(provider.execute, request) for _ in range(2)]
        entered.wait(timeout=2)
        release.set()
        assert all(build.result(timeout=2).ok for build in builds)
    assert provider.max_builds_global == 4
    assert provider.max_builds_per_project == 2


def test_vite_rejects_absolute_import_outside_sealed_roots(tmp_path):
    snapshot = SafeSnapshotBuilder(registry.FRONTEND_PROJECTS_DIR, registry.BACKEND_PROJECTS_DIR).build("demo-ping", tmp_path / "snapshot")
    canary = tmp_path / "outside-canary.js"
    canary.write_text("export default 'SECRET_CANARY'", encoding="utf-8")
    component = snapshot.root / "frontend" / "src" / "projects" / "demo-ping" / "DemoPing.vue"
    component.write_text(
        f"<template><div>{{{{ leaked }}}}</div></template><script setup>import leaked from {json.dumps(canary.as_posix())};</script>",
        encoding="utf-8",
    )
    with pytest.raises(BuildFailed) as error:
        LocalSubprocessBuildProvider(registry.FRONTEND_PROJECTS_DIR.parents[1]).execute(
            BuildRequest("demo-ping", snapshot.root, tmp_path / "escape-output", 120)
        )
    assert error.value.code == "vite_build_failed"
    output = tmp_path / "escape-output" / "preview"
    assert not output.exists() or all(b"SECRET_CANARY" not in path.read_bytes() for path in output.rglob("*") if path.is_file())
