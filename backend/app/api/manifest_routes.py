"""Manifest routes — backs ``PlanManifestPanel.vue`` (design.md §2.5/§4.3),
blueprint prefix ``/api/manifest``.

These are thin Studio-authored Flask view functions over the VENDORED
``manifest_lookup.py`` (parsing/caching) and ``routingtools_client.py``
(Route Planning cookie/manifest-fetch) functions already copied verbatim
(FEAT-001) — Studio does not reimplement manifest parsing or the Route
Planning handshake, it only writes the HTTP plumbing around them, mirroring
how ToolBox's own equivalent route file is thin plumbing over the same
functions.
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, request

manifest_bp = Blueprint("manifest", __name__)


@manifest_bp.get("/cycles")
def get_cycles():
    from vendor.integrations.routingtools_client import ALLOWED_CYCLES, DEFAULT_CYCLE

    return jsonify({"cycles": list(ALLOWED_CYCLES), "default": DEFAULT_CYCLE})


@manifest_bp.get("/cookies-status")
def get_cookies_status():
    from vendor.integrations.routingtools_client import cookies_status

    try:
        status = cookies_status()
    except Exception:  # noqa: BLE001 — probe must never 500
        return jsonify({"ready": False, "login_url": None, "error": "probe_failed"})
    return jsonify(status)


def _parse_date_param() -> date | None:
    raw = request.args.get("date", "")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@manifest_bp.get("/status")
def get_manifest_status():
    from vendor.integrations.manifest_lookup import find_daily_manifest

    when = _parse_date_param()
    station_code = request.args.get("station_code") or None
    cycle = request.args.get("cycle") or None

    try:
        found = find_daily_manifest(when, station_code=station_code, cycle=cycle)
    except Exception:  # noqa: BLE001 — never 500 on a cache-read failure
        found = None

    if found is None:
        return jsonify({"present": False, "path": None})
    return jsonify({"present": True, "path": str(found)})


@manifest_bp.post("/upload")
def upload_manifest():
    from vendor.integrations.manifest_lookup import persist_uploaded_manifest

    uploaded = request.files.get("file")
    if uploaded is None:
        return jsonify({"error": "missing_file"}), 400

    when = _parse_date_param()
    station_code = request.form.get("station_code") or None
    cycle = request.form.get("cycle") or None

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / (uploaded.filename or "manifest.xlsx")
        uploaded.save(tmp_path)
        try:
            destination = persist_uploaded_manifest(
                tmp_path, when=when, station_code=station_code, cycle=cycle
            )
        except Exception:  # noqa: BLE001 — upload must never 500
            destination = None

    if destination is None:
        return jsonify({"success": False})
    return jsonify({"success": True, "path": str(destination)})


@manifest_bp.post("/fetch")
def fetch_manifest():
    from vendor.integrations.routingtools_client import (
        RoutingToolsAuthError,
        RoutingToolsClientError,
        download_plan_manifest,
    )
    from vendor.integrations.manifest_lookup import persist_manifest_bytes

    body = request.get_json(silent=True) or {}
    station_code = body.get("station_code", "")
    cycle = body.get("cycle")
    plan_date = body.get("date")

    if not station_code:
        return jsonify({"success": False, "error": "missing_station_code"}), 400

    try:
        downloaded = download_plan_manifest(
            station_code=station_code, cycle=cycle, plan_date=plan_date
        )
    except RoutingToolsAuthError as exc:
        return jsonify({"success": False, "error": "auth_required", "detail": str(exc)})
    except RoutingToolsClientError as exc:
        return jsonify({"success": False, "error": "fetch_failed", "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001 — never 500 on a fetch failure
        return jsonify({"success": False, "error": "fetch_failed", "detail": str(exc)})

    try:
        destination = persist_manifest_bytes(
            downloaded.content, station_code=station_code, cycle=cycle
        )
    except Exception:  # noqa: BLE001 — never 500 on a cache-write failure
        destination = None

    return jsonify(
        {
            "success": True,
            "filename": downloaded.filename,
            "path": str(destination) if destination else None,
        }
    )
