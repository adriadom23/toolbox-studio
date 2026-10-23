"""Scaffolded Flask blueprint for the "k" Toolbox Studio project.

Import vendored integration clients the same way ToolBox's own
`app/tools/*` modules do, e.g.:

    from vendor.integrations import scc_client
    from vendor.integrations.slack_client import post_message

See ToolboxStudio/README.md and the Component Catalog inside Studio's
UI for what's available.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

blueprint = Blueprint("project_k", __name__)


@blueprint.get("/ping")
def ping():
    """Minimal health route the scaffolded .vue calls on load.

    Replace with your project's real routes. Keep the blueprint object
    named `blueprint` — Toolbox Studio's dynamic registration
    (app/projects/registry.py) looks for that exact name.
    """
    return jsonify({"status": "ok", "project": "k"})
