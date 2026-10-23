"""Filesystem-backed project listing/metadata and dynamic blueprint
registration (design.md §4.2/§4.3).

A project exists iff BOTH halves exist on disk:
``frontend/src/projects/<name>/`` and ``backend/app/projects/<name>/``
(see §6 scaffold). Project metadata (``created_at``/``modified_at``/
``status``) comes from a small ``project.meta.json`` file written at
scaffold time and touched on save — not from filesystem mtimes, which a
git checkout/clone would scramble.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.projects.safe_fs import is_safe_directory, stable_directories
from app.projects.validation import InvalidProjectNameError, validate_slug
from app.submissions.snapshot import _open_regular_nofollow

logger = logging.getLogger(__name__)

# ``app/projects/registry.py`` -> ``app/projects`` -> ``app`` -> ``backend``
BACKEND_DIR = Path(__file__).resolve().parents[2]
BACKEND_PROJECTS_DIR = BACKEND_DIR / "app" / "projects"
FRONTEND_PROJECTS_DIR = BACKEND_DIR.parent / "frontend" / "src" / "projects"

META_FILENAME = "project.meta.json"
_MAX_META_BYTES = 64 * 1024

# Names that are not projects even though they live under
# ``app/projects/`` (Python package plumbing / this module's own siblings).
_RESERVED_NAMES = {"__pycache__", "__init__.py", "registry.py", "scaffold.py", "packaging.py"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def backend_project_dir(name: str) -> Path:
    return BACKEND_PROJECTS_DIR / name


def frontend_project_dir(name: str) -> Path:
    return FRONTEND_PROJECTS_DIR / name


def meta_path(name: str) -> Path:
    return frontend_project_dir(name) / META_FILENAME


@contextmanager
def stable_project_roots(name: str) -> Iterator[None]:
    slug = validate_slug(name)
    with stable_directories(frontend_project_dir(slug), backend_project_dir(slug)):
        yield


def project_exists(name: str) -> bool:
    try:
        validate_slug(name)
    except InvalidProjectNameError:
        return False
    return is_safe_directory(backend_project_dir(name)) and is_safe_directory(frontend_project_dir(name))


def read_meta(name: str) -> dict[str, Any]:
    """Read bounded regular metadata without following links."""

    path = meta_path(name)
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_META_BYTES
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        ):
            raise OSError("project metadata is not a bounded regular file")
        with _open_regular_nofollow(path) as (handle, opened):
            if opened.st_size > _MAX_META_BYTES:
                raise OSError("project metadata exceeds 64 KiB")
            payload = handle.read(_MAX_META_BYTES + 1)
        if len(payload) > _MAX_META_BYTES:
            raise OSError("project metadata exceeds 64 KiB")
        data = json.loads(payload.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return {
        "name": name,
        "displayName": name,
        "created_at": None,
        "modified_at": None,
        "status": "draft",
    }


def write_meta(name: str, meta: dict[str, Any]) -> None:
    """Atomically replace metadata without opening the destination link."""

    path = meta_path(name)
    parent = path.parent
    parent_info = parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or bool(getattr(parent_info, "st_file_attributes", 0) & 0x400)
    ):
        raise OSError("project metadata directory is unsafe")
    payload = (json.dumps(meta, indent=2) + "\n").encode("utf-8")
    if len(payload) > _MAX_META_BYTES:
        raise ValueError("project metadata exceeds 64 KiB")
    staging = parent / f".{META_FILENAME}.staging-{os.getpid()}-{secrets.token_hex(8)}"
    with staging.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)


def touch_modified(name: str) -> dict[str, Any]:
    """Update ``modified_at`` in the project's metadata and return it."""

    meta = read_meta(name)
    meta["modified_at"] = now_iso()
    write_meta(name, meta)
    return meta


def set_status(name: str, status: str) -> dict[str, Any]:
    meta = read_meta(name)
    meta["status"] = status
    meta["modified_at"] = now_iso()
    write_meta(name, meta)
    return meta


def list_projects() -> list[dict[str, Any]]:
    """Return ``[{name, created_at, modified_at, status}]`` for every
    project present on disk (both halves)."""

    if not BACKEND_PROJECTS_DIR.is_dir():
        return []

    names: list[str] = []
    for entry in sorted(BACKEND_PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in _RESERVED_NAMES or entry.name.startswith("_"):
            continue
        if project_exists(entry.name):
            names.append(entry.name)

    out: list[dict[str, Any]] = []
    for name in names:
        meta = read_meta(name)
        out.append(
            {
                "name": name,
                "created_at": meta.get("created_at"),
                "modified_at": meta.get("modified_at"),
                "status": meta.get("status", "draft"),
            }
        )
    return out


def register_all_project_blueprints(app) -> None:
    """Walk ``app/projects/<name>/routes.py`` for every project on disk,
    import each module dynamically, and register its exported
    ``blueprint`` object under ``/api/projects/<name>/app``.

    This is the mechanism that makes a scaffolded project's backend route
    live and callable the moment the project exists on disk — no manual
    registration step (design.md §4.2/§6.4).

    A project whose ``routes.py`` fails to import (syntax error while the
    developer is mid-edit) is logged and skipped, not a crash of the whole
    Studio backend (design.md §4.6) — it still appears in ``GET
    /api/projects`` (listed from disk, independent of import success).
    """

    if not BACKEND_PROJECTS_DIR.is_dir():
        return

    for entry in sorted(BACKEND_PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in _RESERVED_NAMES or entry.name.startswith("_"):
            continue
        name = entry.name
        if not project_exists(name):
            continue
        routes_path = entry / "routes.py"
        if not routes_path.exists():
            continue
        try:
            module = importlib.import_module(f"app.projects.{name}.routes")
            blueprint = getattr(module, "blueprint")
        except Exception as exc:  # noqa: BLE001 — never crash Studio's own boot
            logger.warning("project %s failed to import: %s", name, exc)
            continue
        try:
            app.register_blueprint(blueprint, url_prefix=f"/api/projects/{name}/app")
        except Exception as exc:  # noqa: BLE001 — e.g. duplicate registration on reload
            logger.warning("project %s failed to register: %s", name, exc)
