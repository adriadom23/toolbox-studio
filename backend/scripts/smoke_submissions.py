from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
FRONTEND_ROOT = REPOSITORY_ROOT / "frontend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import asgi as asgi_module  # noqa: E402
from app.api.routers import preview as preview_router_module  # noqa: E402
from app.core.studio_config import load_studio_config  # noqa: E402
from app.preview_app import create_preview_app  # noqa: E402
from app.projects import registry, scaffold  # noqa: E402
from app.submissions.build import LocalSubprocessBuildProvider  # noqa: E402
from app.submissions.provider_factory import create_local_providers  # noqa: E402

PREVIEW_PORT = 5001


class RepositoryFrontendBuildProvider(LocalSubprocessBuildProvider):
    """Compile isolated smoke projects with this checkout's pinned frontend."""

    def __init__(self, _frontend_root: Path, *args, **kwargs) -> None:
        super().__init__(FRONTEND_ROOT, *args, **kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_available(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _start_server(app, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, proxy_headers=False))
    thread = threading.Thread(target=server.run, name=f"smoke-http-{port}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"el servidor HTTP del humo no arrancó en el puerto {port}")
    return server, thread


def _stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        raise RuntimeError("un servidor HTTP propio no terminó en el plazo esperado")


def _assert_response(response: httpx.Response, expected: int, step: str) -> dict:
    if response.status_code != expected:
        raise AssertionError(f"{step}: HTTP {response.status_code}: {response.text[:1000]}")
    if not response.content:
        return {}
    return response.json()


def _csrf(client: httpx.Client) -> dict[str, str]:
    token = client.cookies.get("studio_csrf")
    if not token:
        raise AssertionError("la sesión local no entregó cookie CSRF")
    return {"x-csrf-token": token}


def run_smoke(runtime_root: Path | None = None) -> dict[str, object]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = Path(runtime_root or BACKEND_ROOT / "data" / "runtime" / "smoke" / f"run-{stamp}")
    frontend_projects = root / "workspace" / "frontend-projects"
    backend_projects = root / "workspace" / "backend-projects"
    frontend_projects.mkdir(parents=True, exist_ok=False)
    backend_projects.mkdir(parents=True, exist_ok=False)
    database_path = root / "studio.sqlite3"
    config_path = root / "studio.config.json"
    api_port = _free_port()
    config_path.write_text(json.dumps({
        "schema_version": 2,
        "server": {
            "profile": "local", "bind": "127.0.0.1", "port": api_port,
            "public_base_url": "http://127.0.0.1:5173",
            "cors_origins": ["http://127.0.0.1:5173"],
        },
        "runtime": {"root": str(root), "database_path": str(database_path)},
        "identity": {"mapping_version": 1, "mapping": {}, "roles": {"local-user": ["admin", "reviewer", "submitter"]}},
        "providers": {
            "artifact": "local", "build": "local-subprocess", "logs": "local",
            "notifications": ["local"], "publication": "local", "runtime": "local-subprocess",
        },
    }, indent=2) + "\n", encoding="utf-8")

    if not _port_available(PREVIEW_PORT):
        raise RuntimeError("el puerto 5001 está ocupado; el humo no puede verificar el origen aislado de preview")

    originals = (
        registry.FRONTEND_PROJECTS_DIR,
        registry.BACKEND_PROJECTS_DIR,
        scaffold.FRONTEND_PROJECTS_DIR,
        scaffold.BACKEND_PROJECTS_DIR,
        preview_router_module.LocalSubprocessBuildProvider,
    )
    registry.FRONTEND_PROJECTS_DIR = frontend_projects
    registry.BACKEND_PROJECTS_DIR = backend_projects
    scaffold.FRONTEND_PROJECTS_DIR = frontend_projects
    scaffold.BACKEND_PROJECTS_DIR = backend_projects
    preview_router_module.LocalSubprocessBuildProvider = RepositoryFrontendBuildProvider

    api_server = preview_server = None
    api_thread = preview_thread = None
    try:
        config = load_studio_config(config_path)
        providers = replace(
            create_local_providers(root, FRONTEND_ROOT, BACKEND_ROOT),
            build=RepositoryFrontendBuildProvider(FRONTEND_ROOT),
        )
        api_app = asgi_module.create_app(config, providers=providers)
        preview_app = create_preview_app(config=config, database_path=database_path, preview_root=root / "previews")
        api_server, api_thread = _start_server(api_app, api_port)
        preview_server, preview_thread = _start_server(preview_app, PREVIEW_PORT)

        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=120) as client:
            _assert_response(client.get("/api/ready"), 200, "ready")
            _assert_response(client.get("/api/session"), 200, "sesión")
            headers = _csrf(client)

            created = _assert_response(client.post("/api/projects", json={"name": "Smoke Submission"}, headers={**headers, "Idempotency-Key": "smoke-project-create"}), 201, "crear")
            slug = str(created["name"])
            detail = _assert_response(client.get(f"/api/projects/{slug}"), 200, "detalle de proyecto")
            component = next(item for item in detail["files"] if item["path"].endswith(".vue"))
            edited_content = """<template>\n  <main><h1>Toolbox Studio</h1><p>Flujo HTTP real verificado.</p></main>\n</template>\n\n<style scoped>\nmain { font-family: sans-serif; padding: 2rem; }\n</style>\n"""
            _assert_response(client.put(f"/api/projects/{slug}/files", json={"files": [{"path": component["path"], "content": edited_content, "base_sha256": component["sha256"]}]}, headers=headers), 200, "editar")

            preview_operation = _assert_response(
                client.post(f"/api/projects/{slug}/preview-builds", headers={**headers, "Idempotency-Key": f"smoke-preview-{stamp}"}),
                202,
                "reservar preview",
            )
            preview_deadline = time.monotonic() + 120
            while preview_operation.get("state") in {"queued", "running"} and time.monotonic() < preview_deadline:
                time.sleep(0.2)
                preview_operation = _assert_response(client.get(preview_operation["poll_url"]), 200, "consultar preview")
            if preview_operation.get("state") != "ready":
                raise AssertionError(f"el preview no terminó en ready: {preview_operation}")
            preview = preview_operation
            grant = _assert_response(client.post("/api/preview-grants", json={"subject_kind": "workspace", "subject_id": slug, "preview_id": preview["preview_id"]}, headers=headers), 201, "crear grant")
            with httpx.Client(base_url=f"http://127.0.0.1:{PREVIEW_PORT}", timeout=30) as preview_client:
                exchange = _assert_response(preview_client.post("/exchange", json={"nonce": grant["nonce"], "preview_id": grant["preview_id"], "subject_kind": "workspace", "subject_id": slug}), 200, "intercambiar grant")
                asset = preview_client.get(exchange["url"])
                if asset.status_code != 200 or "<script" not in asset.text:
                    raise AssertionError("el preview aislado no sirvió su index.html sellado")
                runtime_response = preview_client.get(f"/api/projects/{slug}/app/ping")
                if runtime_response.status_code != 200:
                    raise AssertionError(f"el proxy runtime sellado falló: HTTP {runtime_response.status_code}: {runtime_response.text[:500]}")

            reserved = _assert_response(client.post("/api/submissions", json={"project_slug": slug, "title": "Humo integral", "description": "Compilación, revisión y publicación local."}, headers={**headers, "Idempotency-Key": f"smoke-{stamp}"}), 202, "enviar")
            operation_id = str(reserved["operation_id"])
            operation = reserved
            deadline = time.monotonic() + 120
            while operation.get("status") == "building" and time.monotonic() < deadline:
                time.sleep(0.2)
                operation = _assert_response(client.get(f"/api/submission-operations/{operation_id}"), 200, "consultar operación")
            if operation.get("status") != "submitted":
                raise AssertionError(f"la compilación no terminó en submitted: {operation}")
            submission_id = str(operation["submission_id"])

            listing = _assert_response(client.get("/api/submissions", params={"project_slug": slug}), 200, "listar")
            if not any(item["id"] == submission_id for item in listing["items"]):
                raise AssertionError("la entrega no apareció en la bandeja")
            submission = _assert_response(client.get(f"/api/submissions/{submission_id}"), 200, "detalle de entrega")
            logs = _assert_response(client.get(f"/api/submissions/{submission_id}/logs", params={"phase": "frontend"}), 200, "log")
            if not isinstance(logs.get("lines"), list):
                raise AssertionError("la API de logs no devolvió líneas")
            comment = _assert_response(client.post(f"/api/submissions/{submission_id}/comments", json={"text": "Revisión de humo."}, headers=headers), 201, "comentar")
            if comment.get("body") != "Revisión de humo.":
                raise AssertionError("el comentario no se persistió")
            started = _assert_response(client.post(f"/api/submissions/{submission_id}/review", json={"action": "start", "expected_row_version": submission["row_version"]}, headers=headers), 200, "iniciar revisión")
            approved = _assert_response(client.post(f"/api/submissions/{submission_id}/review", json={"action": "approve", "expected_row_version": started["row_version"], "publication_target": "local-index"}, headers=headers), 200, "aprobar y publicar")
            if approved.get("state") != "published":
                published = _assert_response(client.post(f"/api/submissions/{submission_id}/publish", json={"expected_row_version": approved["row_version"], "target": "local-index"}, headers=headers), 200, "publicar")
            else:
                published = {"status": approved["state"], "row_version": approved["row_version"]}
            if published.get("status") != "published":
                raise AssertionError(f"la publicación local no terminó en published: {published}")

            final_detail = _assert_response(client.get(f"/api/submissions/{submission_id}"), 200, "detalle final")
            artifact = client.get(f"/api/submissions/{submission_id}/artifact")
            if artifact.status_code != 200:
                raise AssertionError(f"descargar: HTTP {artifact.status_code}: {artifact.text[:1000]}")
            downloaded_sha = hashlib.sha256(artifact.content).hexdigest()
            if downloaded_sha != final_detail["artifact_sha256"]:
                raise AssertionError("el SHA-256 descargado no coincide con el registrado")

            result = {
                "status": final_detail["state"],
                "project_slug": slug,
                "submission_id": submission_id,
                "preview_id": preview["preview_id"],
                "artifact_sha256": downloaded_sha,
                "runtime_root": str(root),
                "boto3_loaded": "boto3" in sys.modules,
            }
            if result["status"] != "published" or result["boto3_loaded"]:
                raise AssertionError(f"resultado local inesperado: {result}")
            return result
    finally:
        if preview_server and preview_thread:
            _stop_server(preview_server, preview_thread)
        if api_server and api_thread:
            _stop_server(api_server, api_thread)
        (
            registry.FRONTEND_PROJECTS_DIR,
            registry.BACKEND_PROJECTS_DIR,
            scaffold.FRONTEND_PROJECTS_DIR,
            scaffold.BACKEND_PROJECTS_DIR,
            preview_router_module.LocalSubprocessBuildProvider,
        ) = originals


def main() -> None:
    result = run_smoke()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
