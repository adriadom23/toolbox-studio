from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path


def emit(ok: bool, code: str) -> int:
    print(json.dumps({"version": 1, "ok": ok, "checks": [{"code": code, "status": "passed" if ok else "failed"}]}, separators=(",", ":")))
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) != 3 or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", sys.argv[2]):
        return emit(False, "invalid_arguments")
    snapshot = Path(sys.argv[1]).resolve(strict=True)
    slug = sys.argv[2]
    routes = snapshot / "backend" / "app" / "projects" / slug / "routes.py"
    if not routes.is_file() or routes.is_symlink():
        return emit(False, "routes_missing")
    sys.path.insert(0, str(snapshot / "backend"))
    try:
        from flask import Blueprint, Flask
        module_name = "submission_project_routes"
        spec = importlib.util.spec_from_file_location(module_name, routes)
        if spec is None or spec.loader is None:
            return emit(False, "import_failed")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        blueprint = getattr(module, "blueprint", None)
        if not isinstance(blueprint, Blueprint):
            return emit(False, "blueprint_missing")
        rules = list(blueprint.deferred_functions)
        if not rules:
            return emit(False, "routes_missing")
        app = Flask("submission_validator")
        app.register_blueprint(blueprint, url_prefix="/_project")
        response = app.test_client().get("/_project/ping")
        if not 200 <= response.status_code < 300 or not isinstance(response.get_json(silent=True), dict):
            return emit(False, "ping_failed")
    except Exception:
        return emit(False, "import_failed")
    return emit(True, "blueprint_ping")


if __name__ == "__main__":
    raise SystemExit(main())
