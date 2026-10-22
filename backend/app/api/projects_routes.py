"""Project CRUD API (design.md §4.3's CRUD table), blueprint prefix
``/api/projects``.
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, request

from app.projects import registry, scaffold

projects_bp = Blueprint("projects", __name__)


@projects_bp.get("", strict_slashes=False)
def list_projects():
    return jsonify(registry.list_projects())


@projects_bp.post("", strict_slashes=False)
def create_project():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    try:
        meta = scaffold.create_project(name)
    except scaffold.InvalidProjectNameError as exc:
        return jsonify({"error": "invalid_name", "detail": str(exc)}), 400
    except scaffold.ProjectAlreadyExistsError:
        return jsonify({"error": "name_exists"}), 409
    except OSError as exc:
        return jsonify({"error": "filesystem_error", "detail": str(exc)}), 500
    return jsonify(meta), 201


@projects_bp.get("/<name>")
def get_project(name: str):
    if not registry.project_exists(name):
        return jsonify({"error": "not_found"}), 404

    files = []
    for base, label in (
        (registry.frontend_project_dir(name), "frontend"),
        (registry.backend_project_dir(name), "backend"),
    ):
        for file_path in sorted(base.rglob("*")):
            if file_path.is_dir():
                continue
            rel = file_path.relative_to(base)
            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            files.append({"path": f"{label}/{rel.as_posix()}", "content": content})

    return jsonify({"meta": registry.read_meta(name), "files": files})


def _resolve_file_path(name: str, rel_path: str) -> Path | None:
    """Resolve ``rel_path`` (e.g. ``"frontend/Foo.vue"``) to an absolute path
    guaranteed to live inside the project's own two folders, or ``None`` when
    it would escape them (path-traversal guard, design.md §4.3/§4.7)."""

    if not rel_path or "\x00" in rel_path:
        return None
    parts = Path(rel_path).parts
    if not parts or parts[0] not in ("frontend", "backend"):
        return None
    base = (
        registry.frontend_project_dir(name)
        if parts[0] == "frontend"
        else registry.backend_project_dir(name)
    )
    sub_rel = Path(*parts[1:]) if len(parts) > 1 else Path(".")
    try:
        candidate = (base / sub_rel).resolve()
        base_resolved = base.resolve()
    except OSError:
        return None
    if base_resolved not in candidate.parents and candidate != base_resolved:
        return None
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        return None
    return candidate


@projects_bp.put("/<name>/files")
def save_project_files(name: str):
    if not registry.project_exists(name):
        return jsonify({"error": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    files = body.get("files") or []

    # Path-traversal guard: validate EVERY file first, reject the whole
    # request on the first bad path (no partial writes) — design.md §4.7.
    resolved: list[tuple[Path, str]] = []
    for item in files:
        rel_path = str(item.get("path", ""))
        content = item.get("content", "")
        target = _resolve_file_path(name, rel_path)
        if target is None:
            return jsonify({"error": "path_outside_project", "detail": rel_path}), 400
        resolved.append((target, content))

    try:
        for target, content in resolved:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return jsonify({"error": "filesystem_error", "detail": str(exc)}), 500

    meta = registry.touch_modified(name)
    return jsonify(meta)


@projects_bp.delete("/<name>")
def delete_project(name: str):
    if not registry.project_exists(name):
        return jsonify({"error": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "confirm_required"}), 400

    import shutil

    try:
        shutil.rmtree(registry.frontend_project_dir(name))
        shutil.rmtree(registry.backend_project_dir(name))
    except OSError as exc:
        return jsonify({"error": "filesystem_error", "detail": str(exc)}), 500

    return jsonify({"status": "deleted", "name": name})
