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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ``app/projects/registry.py`` -> ``app/projects`` -> ``app`` -> ``backend``
BACKEND_DIR = Path(__file__).resolve().parents[2]
BACKEND_PROJECTS_DIR = BACKEND_DIR / "app" / "projects"
FRONTEND_PROJECTS_DIR = BACKEND_DIR.parent / "frontend" / "src" / "projects"

META_FILENAME = "project.meta.json"

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


def project_exists(name: str) -> bool:
    return backend_project_dir(name).is_dir() and frontend_project_dir(name).is_dir()


def read_meta(name: str) -> dict[str, Any]:
    """Return the project's ``project.meta.json`` contents, or a minimal
    fallback shape when the file is missing/unreadable (never raises)."""

    path = meta_path(name)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {
        "name": name,
        "displayName": name,
        "created_at": None,
        "modified_at": None,
        "status": "draft",
    }


def write_meta(name: str, meta: dict[str, Any]) -> None:
    path = meta_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
        handle.write("\n")


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
