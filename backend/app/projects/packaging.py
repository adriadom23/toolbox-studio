"""Compatibility facade over the sealed, reproducible submission packager."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from app.projects.registry import BACKEND_DIR, BACKEND_PROJECTS_DIR, FRONTEND_PROJECTS_DIR
from app.submissions.packaging import create_reproducible_package, verify_package
from app.submissions.snapshot import SafeSnapshotBuilder


class ProjectNotFoundError(ValueError):
    pass


SUBMISSIONS_DIR = BACKEND_DIR / "data" / "runtime" / "compat-submissions"


def build_package(project_name: str) -> Path:
    if not (FRONTEND_PROJECTS_DIR / project_name).is_dir() or not (BACKEND_PROJECTS_DIR / project_name).is_dir():
        raise ProjectNotFoundError(project_name)
    project_dir = SUBMISSIONS_DIR / project_name
    snapshot_dir = project_dir / "snapshots" / uuid.uuid4().hex
    try:
        snapshot = SafeSnapshotBuilder(FRONTEND_PROJECTS_DIR, BACKEND_PROJECTS_DIR).build(project_name, snapshot_dir)
    except Exception as exc:
        if getattr(exc, "code", None) == "not_found":
            raise ProjectNotFoundError(project_name) from exc
        raise
    destination = project_dir / f"{project_name}-{snapshot.input_sha256[:16]}.zip"
    if destination.is_file():
        verify_package(destination)
        return destination
    result = create_reproducible_package(snapshot, destination, submission_id=f"legacy-{snapshot.input_sha256[:16]}", version=1, harness_sha256="legacy-facade")
    return result.path


def _git_config_value(key: str) -> str | None:
    try:
        result = subprocess.run(["git", "config", key], cwd=BACKEND_DIR.parent, capture_output=True, text=True, timeout=5, shell=False, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value or None


def default_submitted_by() -> str | None:
    name = _git_config_value("user.name")
    email = _git_config_value("user.email")
    if name and email:
        return f"{name} <{email}>"
    return name or email


def notify_owners(project_name: str, package_path: Path, submitted_by: str | None = None) -> dict[str, bool]:
    """Deprecated facade: durable notifications are handled by the saga outbox.

    Keeping this side-effect-free prevents legacy callers from performing network
    sends while preserving the old return shape.
    """
    del project_name, package_path, submitted_by
    return {"email": False, "slack": False}
