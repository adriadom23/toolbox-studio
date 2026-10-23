from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.projects.validation import InvalidProjectNameError, validate_slug
from app.submissions.database import transaction
from app.submissions.repository import SubmissionRepository

_MAX_META_BYTES = 64 * 1024
_META_FIELDS = {"name", "displayName", "created_at", "modified_at", "status"}


def _read_meta(project_root: Path, slug: str) -> dict[str, object]:
    path = project_root / "project.meta.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size > _MAX_META_BYTES
        or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    ):
        raise ValueError("project metadata must be a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(_MAX_META_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_META_BYTES:
        raise ValueError("project metadata exceeds 64 KiB")
    raw = json.loads(content.decode("utf-8"))
    if not isinstance(raw, dict) or set(raw) - _META_FIELDS:
        raise ValueError("project metadata has unsupported fields")
    if "name" in raw and raw["name"] != slug:
        raise ValueError("project metadata name does not match its directory")
    display_name = raw.get("displayName") or raw.get("name") or slug
    if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 200:
        raise ValueError("project display name is invalid")
    if raw.get("status", "draft") not in {"draft", "submitted"}:
        raise ValueError("project metadata status is invalid")
    if any(raw.get(field) is not None and not isinstance(raw.get(field), str) for field in ("created_at", "modified_at")):
        raise ValueError("project metadata timestamps are invalid")
    return raw


def bootstrap_projects(
    frontend_root: Path,
    backend_root: Path,
    repository: SubmissionRepository,
    *,
    local_identity: str | None = None,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    with transaction(repository.connection):
        repository.connection.execute(
            "UPDATE project_bootstrap_diagnostics SET active=0,resolved_at=?,last_seen_at=? WHERE active=1",
            (now, now),
        )
        names = {
            path.name
            for root in (frontend_root, backend_root)
            if root.is_dir()
            for path in root.iterdir()
            if (path.is_dir() or path.is_symlink()) and path.name.casefold() not in {"__pycache__", ".pytest_cache"}
        }

        def record(name: str, status: str) -> None:
            diagnostics.append({"name": name, "status": status})
            if status in {"imported", "existing"}:
                return
            repository.connection.execute(
                "INSERT INTO project_bootstrap_diagnostics(name,status,active,first_seen_at,last_seen_at,resolved_at) "
                "VALUES (?,?,1,?,?,NULL) ON CONFLICT(name) DO UPDATE SET status=excluded.status,active=1,last_seen_at=excluded.last_seen_at,resolved_at=NULL",
                (name, status, now, now),
            )

        discovered: list[str] = []
        for name in sorted(names):
            frontend = frontend_root / name
            backend = backend_root / name
            frontend_info = frontend.lstat() if frontend.exists() or frontend.is_symlink() else None
            backend_info = backend.lstat() if backend.exists() or backend.is_symlink() else None
            if (
                frontend.is_symlink()
                or backend.is_symlink()
                or bool(getattr(frontend_info, "st_file_attributes", 0) & 0x400)
                or bool(getattr(backend_info, "st_file_attributes", 0) & 0x400)
            ):
                record(name, "symlink_rejected")
                continue
            if not frontend.is_dir() or not backend.is_dir():
                record(name, "incomplete")
                continue
            try:
                validate_slug(name)
            except InvalidProjectNameError:
                record(name, "invalid_slug")
                continue
            try:
                raw = _read_meta(frontend, name)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                record(name, "invalid_meta")
                continue
            created = repository.upsert_project(
                slug=name,
                display_name=str(raw.get("displayName") or raw.get("name") or name),
                created_at=raw.get("created_at"),
                modified_at=raw.get("modified_at"),
                status=str(raw.get("status") or "draft"),
            )
            repository.append_event(project_slug=name, event_type="discovered", actor="bootstrap", dedupe_key=f"bootstrap:{name}")
            record(name, "imported" if created else "existing")
            discovered.append(name)

        if local_identity:
            for name in discovered:
                cursor = repository.connection.execute(
                    "UPDATE projects SET owner_id=?,version=version+1 WHERE slug=? AND owner_id IS NULL",
                    (local_identity, name),
                )
                if cursor.rowcount != 1:
                    continue
                seq = int(repository.connection.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM project_security_events WHERE project_slug=?",
                    (name,),
                ).fetchone()[0])
                repository.connection.execute(
                    "INSERT INTO project_security_events(project_slug,seq,event_type,actor_id,data_json,correlation_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (name, seq, "project_claimed_local", local_identity, "{}", "bootstrap", now),
                )
    return diagnostics
