"""Create-project scaffold generator (design.md §6).

``create_project(name)`` validates/sanitizes the raw name (§6.2), writes
the 4 generated files to a temp staging subfolder, then atomically renames
it into place (§4.6's crash-safety requirement: a failure mid-scaffold
never leaves a partially-created project visible to ``GET /``).
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from app.projects.registry import (
    BACKEND_PROJECTS_DIR,
    FRONTEND_PROJECTS_DIR,
    META_FILENAME,
    now_iso,
    project_exists,
)

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
MAX_SLUG_LEN = 40


class InvalidProjectNameError(ValueError):
    """Raised when a raw project name does not produce a valid slug."""


class ProjectAlreadyExistsError(ValueError):
    """Raised when the derived slug collides with an existing project."""


def slugify(raw_name: str) -> str:
    """Derive the folder/module slug from a raw input name (design.md §6.2).

    Lowercased, non ``[a-z0-9]`` runs collapsed to a single ``-``,
    leading/trailing ``-`` trimmed, truncated to 40 chars. Raises
    :class:`InvalidProjectNameError` when the result does not match
    ``^[a-z][a-z0-9-]*$`` (e.g. empty, or starts with a digit).
    """

    text = str(raw_name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text)
    slug = slug.strip("-")[:MAX_SLUG_LEN]
    if not SLUG_RE.match(slug):
        raise InvalidProjectNameError(
            "Project name must produce a slug matching ^[a-z][a-z0-9-]*$ "
            "(letters/digits/hyphens, must start with a letter)."
        )
    return slug


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


def create_project(name: str) -> dict:
    """Validate ``name``, scaffold the 4 generated files, and return the
    new project's metadata (design.md §6.3/§6.4).

    Raises :class:`InvalidProjectNameError` for a name that fails the slug
    regex and :class:`ProjectAlreadyExistsError` for a case-insensitive
    collision with an existing project folder name.
    """

    slug = slugify(name)
    if project_exists(slug) or any(
        existing.lower() == slug for existing in _existing_names()
    ):
        raise ProjectAlreadyExistsError(f"Project '{slug}' already exists.")

    pascal_name = pascal_case(slug)
    # blueprint_var_name(slug) is the documented §6.2 derivation
    # (`<slug>_bp`); the scaffolded routes.py template always names the
    # exported object literally `blueprint` per §6.4, since that is the
    # exact name `register_all_project_blueprints` looks for.
    blueprint_var_name(slug)

    FRONTEND_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    # Stage both halves in temp directories, then atomically rename into
    # place — a failure mid-write never leaves a partially-created project
    # visible to GET /api/projects (design.md §4.6).
    fe_staging = Path(tempfile.mkdtemp(prefix=f".{slug}-fe-", dir=FRONTEND_PROJECTS_DIR))
    be_staging = Path(tempfile.mkdtemp(prefix=f".{slug}-be-", dir=BACKEND_PROJECTS_DIR))
    try:
        created_at = now_iso()
        (fe_staging / f"{pascal_name}.vue").write_text(
            _vue_template(slug, pascal_name), encoding="utf-8"
        )
        meta = {
            "name": slug,
            "displayName": name,
            "created_at": created_at,
            "modified_at": created_at,
            "status": "draft",
        }
        import json as _json

        (fe_staging / META_FILENAME).write_text(
            _json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )

        (be_staging / "__init__.py").write_text("", encoding="utf-8")
        (be_staging / "routes.py").write_text(
            _routes_py_template(slug), encoding="utf-8"
        )

        fe_final = FRONTEND_PROJECTS_DIR / slug
        be_final = BACKEND_PROJECTS_DIR / slug
        fe_staging.rename(fe_final)
        be_staging.rename(be_final)
    except Exception:
        shutil.rmtree(fe_staging, ignore_errors=True)
        shutil.rmtree(be_staging, ignore_errors=True)
        shutil.rmtree(FRONTEND_PROJECTS_DIR / slug, ignore_errors=True)
        shutil.rmtree(BACKEND_PROJECTS_DIR / slug, ignore_errors=True)
        raise

    return meta


def _existing_names() -> list[str]:
    if not BACKEND_PROJECTS_DIR.is_dir():
        return []
    return [entry.name for entry in BACKEND_PROJECTS_DIR.iterdir() if entry.is_dir()]
