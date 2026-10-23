from __future__ import annotations

import importlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path


def main() -> int:
    sealed_archive = len(sys.argv) > 1 and sys.argv[1] == "--sealed-archive"
    if sealed_archive:
        backend_source = sys.argv[2]
        slug = sys.argv[3]
    else:
        backend_source = str(Path(sys.argv[1]).resolve())
        slug = sys.argv[2]
    request_data = json.loads(sys.stdin.read())
    sys.path.insert(0, backend_source)
    captured = io.StringIO()
    with redirect_stdout(captured):
        module = importlib.import_module(f"app.projects.{slug}.routes")
    if captured.getvalue():
        sys.stderr.write(captured.getvalue())
    blueprint = getattr(module, "blueprint")
    from flask import Flask

    app = Flask("toolbox-project-runtime")
    app.register_blueprint(blueprint, url_prefix=f"/api/projects/{slug}/app")
    path = "/" + str(request_data.get("path", "")).lstrip("/")
    query = str(request_data.get("query", ""))
    target = f"/api/projects/{slug}/app{path}" + (f"?{query}" if query else "")
    response = app.test_client().open(
        target,
        method=str(request_data.get("method", "GET")),
        data=str(request_data.get("body", "")).encode("utf-8"),
        content_type=str(request_data.get("content_type", "")) or None,
    )
    sys.stdout.write(json.dumps({"status": response.status_code, "content_type": response.content_type, "body": response.get_data(as_text=True)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
