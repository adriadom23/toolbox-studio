"""Submit-code endpoint (design.md §4.3/§4.6), same blueprint prefix as
project CRUD: ``/api/projects``.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.projects import packaging, registry

submit_bp = Blueprint("submit", __name__)


@submit_bp.post("/<name>/submit")
def submit_project(name: str):
    if not registry.project_exists(name):
        return jsonify({"error": "not_found"}), 404

    try:
        package_path = packaging.build_package(name)
    except packaging.ProjectNotFoundError:
        return jsonify({"error": "not_found"}), 404
    except Exception as exc:  # noqa: BLE001 — a failed package build never notifies
        return jsonify({"error": "package_failed", "detail": str(exc)}), 500

    body = request.get_json(silent=True) or {}
    submitted_by = body.get("submitted_by") or packaging.default_submitted_by()

    # A notification failure is never a submit failure (§4.6): both
    # send_email/post_message already return False/a non-ok dict rather
    # than raising, but notify_owners is called defensively anyway.
    notified = packaging.notify_owners(name, package_path, submitted_by)

    meta = registry.set_status(name, "submitted")

    return jsonify(
        {
            "status": "submitted",
            "package_path": str(package_path),
            "notified": notified,
            "meta": meta,
        }
    )
