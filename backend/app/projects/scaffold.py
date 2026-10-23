"""Create-project scaffold generator (design.md §6).

``create_project(name)`` validates/sanitizes the raw name (§6.2), writes
the 4 generated files to a temp staging subfolder, then atomically renames
it into place (§4.6's crash-safety requirement: a failure mid-scaffold
never leaves a partially-created project visible to ``GET /``).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from app.projects.safe_fs import (
    is_safe_directory,
    quarantine_path,
    replace_directory_child,
    stable_directories,
    write_journal,
)
from app.projects.validation import (
    MAX_SLUG_LEN,
    SLUG_RE,
    InvalidProjectNameError,
    slugify,
    validate_slug,
)
from app.projects.registry import (
    BACKEND_PROJECTS_DIR,
    FRONTEND_PROJECTS_DIR,
    META_FILENAME,
    now_iso,
    project_exists,
)


class ProjectAlreadyExistsError(ValueError):
    """Raised when the derived slug collides with an existing project."""


@contextmanager
def _creation_lock():
    """Serialize promotion without deleting the persistent lock file."""
    lock_path = BACKEND_PROJECTS_DIR / ".toolbox-scaffold.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def pascal_case(slug: str) -> str:
    """``"dock-health-monitor"`` -> ``"DockHealthMonitor"`` (design.md §6.2)."""

    return "".join(segment.capitalize() for segment in slug.split("-") if segment)


def blueprint_var_name(slug: str) -> str:
    """``"dock-health-monitor"`` -> ``"dock_health_monitor_bp"`` (design.md §6.2)."""

    return f"{slug.replace('-', '_')}_bp"


def _vue_template(slug: str, pascal_name: str) -> str:
    return (
        """<template>
  <div class="tool-surface">
    <header class="tool-header">
      <h2>{{ toolTitle }}</h2>
      <p class="tool-subtitle">
        Scaffolded by Toolbox Studio. Replace this with your tool's UI.
      </p>
    </header>

    <!-- Example: the vendored connections pattern (useIdentity + IdentifyPanel).
         Delete this block if your tool doesn't need an external session. -->
    <CollapsedSection :title="$t('common.connectionsTitle') || 'Conexiones'" start-open>
      <IdentifyPanel :identity="identity" />
    </CollapsedSection>

    <!-- Example: a vendored building block (table sorting + a simple list). -->
    <section class="tool-panel">
      <table class="tb-table">
        <thead>
          <tr>
            <SortTh v-for="col in columns" :key="col.key" :label="col.label" />
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>{{ row.id }}</td>
            <td>{{ row.value }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="tool-empty">No data yet — wire up {{ pingPath }}.</p>
    </section>

    <button class="tb-button tb-button--primary" @click="ping">Ping backend</button>
    <p v-if="pingResult">{{ pingResult }}</p>
  </div>
</template>

<script setup>
import { ref } from "vue";
import CollapsedSection from "../../shared/components/CollapsedSection.vue";
import IdentifyPanel from "../../shared/components/IdentifyPanel.vue";
import SortTh from "../../shared/components/SortTh.vue";
import { useIdentity } from "../../shared/stores/useIdentity";
import { request } from "../../shared/api/http";

const toolTitle = "%(pascal_name)s";
const pingPath = "/projects/%(slug)s/app/ping";

// One vendored connection, as an example. Add more sources from
// shared/stores/globalConnections.js, or remove this entirely if your
// tool doesn't need an external session.
//
// NOTE: useIdentity() self-starts — it probes every source automatically
// on mount (and again on view re-activation) via its own internal
// onMounted(start) hook. You do NOT need to call anything in your own
// onMounted to kick off the first probe; useIdentity has no `probeAll()`
// method. If you want to force a fresh probe later (e.g. after a user
// action), call `identity.refreshAll()`.
const identity = useIdentity([{ id: "scc", label: "SCC" }]);

const columns = [
  { key: "id", label: "ID" },
  { key: "value", label: "Value" },
];
const rows = ref([]);
const pingResult = ref("");

async function ping() {
  try {
    const data = await request(pingPath);
    pingResult.value = JSON.stringify(data);
  } catch (err) {
    pingResult.value = `Error: ${err.message}`;
  }
}
</script>

<style scoped>
.tool-surface {
  max-width: 1320px;
  margin: 0 auto;
  padding: 24px;
}
.tool-header h2 {
  color: var(--toolbox-text);
}
.tool-subtitle {
  color: var(--toolbox-muted);
}
</style>
"""
        % {"pascal_name": pascal_name, "slug": slug}
    )


def _routes_py_template(slug: str) -> str:
    return (
        '''"""Scaffolded Flask blueprint for the "%(slug)s" Toolbox Studio project.

Import vendored integration clients the same way ToolBox's own
`app/tools/*` modules do, e.g.:

    from vendor.integrations import scc_client
    from vendor.integrations.slack_client import post_message

See ToolboxStudio/README.md and the Component Catalog inside Studio's
UI for what's available.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

blueprint = Blueprint("project_%(slug_underscored)s", __name__)


@blueprint.get("/ping")
def ping():
    """Minimal health route the scaffolded .vue calls on load.

    Replace with your project's real routes. Keep the blueprint object
    named `blueprint` — Toolbox Studio's dynamic registration
    (app/projects/registry.py) looks for that exact name.
    """
    return jsonify({"status": "ok", "project": "%(slug)s"})
'''
        % {"slug": slug, "slug_underscored": slug.replace("-", "_")}
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise OSError("scaffold contains a link or reparse point")
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        if stat.S_ISREG(info.st_mode):
            digest.update(str(info.st_size).encode("ascii") + b"\0")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        elif not stat.S_ISDIR(info.st_mode):
            raise OSError("scaffold contains a non-regular entry")
    return digest.hexdigest()


def _journal_path(journal_root: Path, operation_id: str) -> Path:
    if not operation_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in operation_id):
        raise ValueError("invalid project creation operation id")
    return Path(journal_root) / f"{operation_id}.json"


def _write_creation_journal(path: Path, manifest: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("wb") as handle:
        handle.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _validated_creation_items(
    manifest: dict[str, object],
    expected_items: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    slug = validate_slug(str(manifest.get("slug", "")))
    if slug != manifest.get("slug"):
        raise ValueError("project creation journal slug is invalid")
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != 2:
        raise ValueError("invalid project creation journal items")
    by_root: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid project creation journal item")
        root = str(item.get("root", ""))
        staging_name = str(item.get("staging_name", ""))
        target_name = str(item.get("target_name", ""))
        expected_sha256 = str(item.get("sha256", ""))
        prefix = f".{slug}-fe-" if root == "frontend" else f".{slug}-be-" if root == "backend" else ""
        if (
            not prefix
            or root in by_root
            or Path(staging_name).name != staging_name
            or not staging_name.startswith(prefix)
            or len(staging_name) <= len(prefix)
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in staging_name[len(prefix):])
            or target_name != slug
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
            or item.get("status") not in {"prepared", "promoted", "diagnostic"}
        ):
            raise ValueError("invalid project creation journal item")
        by_root[root] = item
    if set(by_root) != {"frontend", "backend"}:
        raise ValueError("project creation journal must contain frontend and backend items")
    if expected_items is not None:
        durable: dict[str, dict[str, object]] = {}
        for item in expected_items:
            kind = str(item.get("item_kind", item.get("root", "")))
            if kind in durable:
                raise ValueError("duplicate durable project creation item")
            durable[kind] = item
        if set(durable) != {"frontend", "backend"}:
            raise ValueError("durable project creation items are incomplete")
        for kind, item in by_root.items():
            expected = durable[kind]
            if (
                str(expected.get("staging_name", "")) != item["staging_name"]
                or str(expected.get("target_name", "")) != item["target_name"]
                or str(expected.get("expected_sha256", expected.get("sha256", ""))) != item["sha256"]
            ):
                raise ValueError("project creation journal differs from durable items")
    return [by_root["frontend"], by_root["backend"]]


def _promote_creation(
    manifest: dict[str, object],
    journal_path: Path,
    expected_items: list[dict[str, object]] | None = None,
) -> dict:
    roots = {"frontend": FRONTEND_PROJECTS_DIR, "backend": BACKEND_PROJECTS_DIR}
    items = _validated_creation_items(manifest, expected_items)
    with stable_directories(*roots.values()), _creation_lock():
        for item in items:
            root = roots[str(item["root"])]
            staging_name = str(item["staging_name"])
            target_name = str(item["target_name"])
            staging = root / staging_name
            target = root / target_name
            expected = str(item["sha256"])
            if target.exists():
                if not is_safe_directory(target) or _tree_digest(target) != expected:
                    raise ProjectAlreadyExistsError(f"Project '{manifest['slug']}' has conflicting content.")
            elif is_safe_directory(staging) and _tree_digest(staging) == expected:
                replace_directory_child(root, staging_name, target_name)
            else:
                raise OSError("project creation staging is missing or changed")
            item["status"] = "promoted"
            _write_creation_journal(journal_path, manifest)
    manifest["status"] = "completed"
    _write_creation_journal(journal_path, manifest)
    return dict(manifest["meta"])


def recover_project_creation(
    operation_id: str,
    journal_root: Path,
    expected_items: list[dict[str, object]] | None = None,
) -> tuple[dict, dict[str, object]] | None:
    path = _journal_path(journal_root, operation_id)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400) or info.st_size > 64 * 1024:
        return None
    from app.submissions.snapshot import _open_regular_nofollow

    with _open_regular_nofollow(path) as (handle, opened):
        if opened.st_size > 64 * 1024:
            raise ValueError("project creation journal is too large")
        payload = handle.read(64 * 1024 + 1)
    if len(payload) > 64 * 1024:
        raise ValueError("project creation journal is too large")
    manifest = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or manifest.get("operation_id") != operation_id
    ):
        raise ValueError("invalid project creation journal")
    _validated_creation_items(manifest, expected_items)
    return _promote_creation(manifest, path, expected_items), manifest


def create_project(
    name: str,
    *,
    operation_id: str | None = None,
    journal_root: Path | None = None,
    prepared_callback: Callable[[dict[str, object]], list[dict[str, object]] | None] | None = None,
) -> dict:
    """Validate ``name``, scaffold the 4 generated files, and return the
    new project's metadata (design.md §6.3/§6.4).

    Raises :class:`InvalidProjectNameError` for a name that fails the slug
    regex and :class:`ProjectAlreadyExistsError` for a case-insensitive
    collision with an existing project folder name.
    """

    slug = slugify(name)
    if project_exists(slug) or any(existing.lower() == slug for existing in _existing_names()):
        raise ProjectAlreadyExistsError(f"Project '{slug}' already exists.")

    pascal_name = pascal_case(slug)
    blueprint_var_name(slug)
    FRONTEND_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    if not is_safe_directory(FRONTEND_PROJECTS_DIR) or not is_safe_directory(BACKEND_PROJECTS_DIR):
        raise OSError("project administrative root is unsafe")

    fe_staging = Path(tempfile.mkdtemp(prefix=f".{slug}-fe-", dir=FRONTEND_PROJECTS_DIR))
    be_staging = Path(tempfile.mkdtemp(prefix=f".{slug}-be-", dir=BACKEND_PROJECTS_DIR))
    created_at = now_iso()
    meta = {
        "name": slug,
        "displayName": name,
        "created_at": created_at,
        "modified_at": created_at,
        "status": "draft",
    }
    try:
        (fe_staging / f"{pascal_name}.vue").write_text(_vue_template(slug, pascal_name), encoding="utf-8")
        (fe_staging / META_FILENAME).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        (be_staging / "__init__.py").write_text("", encoding="utf-8")
        (be_staging / "routes.py").write_text(_routes_py_template(slug), encoding="utf-8")
    except Exception:
        quarantined = []
        for candidate in (fe_staging, be_staging):
            moved = quarantine_path(candidate)
            if moved is not None:
                quarantined.append(str(moved.name))
        write_journal(
            BACKEND_PROJECTS_DIR / f".{slug}.scaffold-journal.json",
            "scaffold_preparation_failed",
            quarantined,
        )
        raise

    operation = operation_id or f"standalone-{secrets.token_hex(16)}"
    root = Path(journal_root or (BACKEND_PROJECTS_DIR / ".toolbox-create-journals"))
    journal = _journal_path(root, operation)
    manifest: dict[str, object] = {
        "version": 1,
        "operation_id": operation,
        "slug": slug,
        "display_name": name,
        "status": "prepared",
        "meta": meta,
        "items": [
            {
                "root": "frontend",
                "staging_name": fe_staging.name,
                "target_name": slug,
                "sha256": _tree_digest(fe_staging),
                "status": "prepared",
            },
            {
                "root": "backend",
                "staging_name": be_staging.name,
                "target_name": slug,
                "sha256": _tree_digest(be_staging),
                "status": "prepared",
            },
        ],
    }
    _write_creation_journal(journal, manifest)
    expected_items = prepared_callback(manifest) if prepared_callback is not None else None
    try:
        return _promote_creation(manifest, journal, expected_items)
    except ProjectAlreadyExistsError:
        manifest["status"] = "diagnostic"
        quarantined = []
        for candidate in (fe_staging, be_staging):
            moved = quarantine_path(candidate)
            if moved is not None:
                quarantined.append(str(moved.name))
        manifest["quarantined_staging"] = quarantined
        _write_creation_journal(journal, manifest)
        raise


def _existing_names() -> list[str]:
    if not BACKEND_PROJECTS_DIR.is_dir():
        return []
    return [entry.name for entry in BACKEND_PROJECTS_DIR.iterdir() if entry.is_dir()]
