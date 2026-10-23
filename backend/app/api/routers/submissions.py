from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from app.api.routers.review import actor_display, actor_roles
from app.api.schemas import LegacySubmissionCreate, SubmissionCreate
from app.auth.authorization import require_workspace
from app.core.errors import PublicError
from app.core.security import decode_cursor, encode_cursor
from app.projects import registry
from app.projects.validation import InvalidProjectNameError, validate_slug
from app.submissions.database import connect
from app.submissions.outbox import effect_retry_state
from app.submissions.providers import ObjectRef
from app.submissions.review import can_view, require_visible
from app.submissions.saga import build_operation_response
from app.submissions.stores import LegacyArtifactStore

_ID_RE = re.compile(r"^sub_[0-9a-f]{32}$")
_OP_RE = re.compile(r"^op_[0-9a-f]{32}$")
_TERMINAL_PREDECESSOR_STATES = {"build_failed", "changes_requested", "rejected", "approved", "published"}
_SORTS = {"created_asc": "created_at ASC,id ASC", "created_desc": "created_at DESC,id DESC", "id_asc": "id ASC", "id_desc": "id DESC"}
_STATES = {"building", "submitted", "under_review", "changes_requested", "rejected", "approved", "published", "build_failed"}
_CURSOR_TTL_SECONDS = 900
_PUBLIC_SUBMISSION_FIELDS = (
    "id", "project_slug", "version_seq", "version_label", "state", "author_id", "author_display",
    "title", "description", "input_sha256", "harness_sha256", "artifact_sha256", "artifact_size",
    "artifact_provider", "legacy", "predecessor_id", "row_version", "correlation_id", "error_code",
    "created_at", "updated_at",
)


def _utc_filter(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicError("invalid_date", f"{field} must be an ISO-8601 UTC timestamp", 422, field=field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PublicError("invalid_date", f"{field} must be an ISO-8601 UTC timestamp", 422, field=field)
    return parsed.astimezone(timezone.utc).isoformat()


def _row_dict(row) -> dict[str, object]:
    keys = set(row.keys())
    result = {key: row[key] for key in _PUBLIC_SUBMISSION_FIELDS if key in keys}
    result["short_id"] = str(row["id"])[-8:]
    result["package_path"] = f"/api/submissions/{row['id']}/artifact" if row["artifact_ref"] else None
    result["manifest_url"] = f"/api/submissions/{row['id']}/manifest"
    result["logs_url"] = f"/api/submissions/{row['id']}/logs"
    result["build_result"] = "failed" if row["state"] == "build_failed" else ("succeeded" if row["artifact_ref"] else "pending")
    return result


def create_submissions_router(saga, providers, *, workspace_guard=None) -> APIRouter:
    router = APIRouter(prefix="/api")

    def authorize_workspace(slug: str, request: Request, capability: str) -> tuple[str, set[str]]:
        try:
            validate_slug(slug)
        except InvalidProjectNameError as exc:
            raise PublicError("invalid_project_slug", "invalid project slug", 422) from exc
        actor, roles = actor_roles(request)
        connection = connect(saga.database_path)
        try:
            require_workspace(connection, slug, actor, roles, capability)
        finally:
            connection.close()
        if workspace_guard is not None:
            workspace_guard(saga.database_path, slug)
        return actor, roles

    @router.get("/projects/{slug}/submission-context")
    def submission_context(slug: str, request: Request):
        actor, roles = authorize_workspace(slug, request, "submission.create")
        if not registry.project_exists(slug):
            raise PublicError("not_found", "project not found", 404)
        connection = connect(saga.database_path)
        try:
            version = int(connection.execute("SELECT COALESCE(MAX(version_seq),0)+1 FROM submission_records WHERE project_slug=?", (slug,)).fetchone()[0])
            predecessors = [
                {"id": row["id"], "version_label": row["version_label"], "state": row["state"]}
                for row in connection.execute(
                    "SELECT id,version_label,state FROM submission_records WHERE project_slug=? AND state IN ('build_failed','changes_requested','rejected','approved','published') ORDER BY version_seq DESC LIMIT 100",
                    (slug,),
                ).fetchall()
            ]
        finally:
            connection.close()
        git = providers.git.inspect(saga.frontend_root.parent).__dict__
        config = request.app.state.config
        warnings = list(git.get("warnings") or ())
        if config.is_local and config.build_provider == "local-subprocess":
            warnings.append("El build local limita recursos, pero no es un sandbox fuerte frente al usuario del sistema operativo.")
        effective_providers = getattr(providers, "identities", None) or {
            "artifact": config.artifact_provider,
            "build": config.build_provider,
            "logs": config.log_provider,
            "notifications": list(config.notification_providers),
            "publication": config.publication_provider,
            "runtime": config.runtime_provider,
        }
        return {
            "project_slug": slug,
            "next_version": version,
            "actor": {"id": actor, "display_name": actor_display(request)},
            "predecessors": predecessors,
            "git": git,
            "providers": effective_providers,
            "warnings": warnings,
            "actions_allowed": ["submission.create"] if roles & {"submitter", "admin"} else [],
        }

    @router.post("/submissions", status_code=202)
    def create_submission(body: SubmissionCreate, request: Request, background: BackgroundTasks, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if idempotency_key is None:
            raise PublicError("idempotency_key_required", "Idempotency-Key is required", 422)
        actor, roles = authorize_workspace(body.project_slug, request, "submission.create")
        reserved, is_new = saga.reserve(
            slug=body.project_slug, actor=actor, actor_roles=roles, author_display=actor_display(request),
            title=body.title.strip(), description=body.description.strip(),
            predecessor_id=body.predecessor_id, idempotency_key=idempotency_key,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
        if is_new:
            background.add_task(saga.run, reserved["operation_id"], reserved["submission_id"])
        return reserved

    @router.post("/projects/{slug}/submit")
    def legacy_submit(slug: str, request: Request, response: Response, body: LegacySubmissionCreate | None = None, prefer: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        actor, roles = authorize_workspace(slug, request, "submission.create")
        supplied = body or LegacySubmissionCreate()
        description = (supplied.description or "").strip() or "Envío creado mediante la API compatible."
        title = (supplied.title or "").strip() or f"Entrega de {slug}"
        key = idempotency_key or "legacy-" + secrets.token_hex(16)
        reserved, is_new = saga.reserve(
            slug=slug, actor=actor, actor_roles=roles, author_display=actor_display(request),
            title=title, description=description, idempotency_key=key,
            correlation_id=getattr(request.state, "correlation_id", None),
            route_template="/api/projects/{slug}/submit",
        )
        if prefer and "respond-async" in prefer.lower():
            if is_new:
                from threading import Thread
                Thread(target=saga.run, args=(reserved["operation_id"], reserved["submission_id"]), daemon=True).start()
            response.status_code = 202 if is_new else saga.operation_status_code(str(reserved["operation_id"]), 202)
            return reserved
        if is_new:
            from threading import Thread
            holder: dict[str, object] = {}

            def execute() -> None:
                holder["result"] = saga.run(reserved["operation_id"], reserved["submission_id"])

            worker = Thread(target=execute, daemon=True)
            worker.start()
            worker.join(saga.submit_wait_seconds)
            if worker.is_alive():
                response.status_code = 504
                return {**reserved, "error": "submit_timeout", "status": "building"}
            result = holder["result"]
        else:
            result = reserved
        response.status_code = saga.operation_status_code(str(reserved["operation_id"]), 202)
        return result

    @router.get("/submission-operations/{operation_id}")
    def operation(operation_id: str, request: Request):
        if not _OP_RE.fullmatch(operation_id):
            raise PublicError("invalid_operation_id", "invalid operation id", 422)
        actor, roles = actor_roles(request)
        connection = connect(saga.database_path)
        try:
            row = connection.execute("SELECT i.operation_id,i.status AS operation_status,i.status_code,i.response_json,s.* FROM idempotency_records i LEFT JOIN submission_records s ON s.id=i.subject_id WHERE i.operation_id=?", (operation_id,)).fetchone()
            if not row or (row["id"] and not can_view(row, actor, roles)):
                raise PublicError("not_found", "operation not found", 404)
            if row["id"]:
                return build_operation_response(connection, operation_id, row)
            if row["response_json"]:
                return json.loads(row["response_json"])
            return {"operation_id": operation_id, "submission_id": None, "status": "building", "poll_url": f"/api/submission-operations/{operation_id}", "phases": []}
        finally:
            connection.close()

    @router.get("/submissions")
    def list_submissions(
        request: Request,
        page_size: int = Query(default=25, ge=1, le=100),
        limit: int | None = Query(default=None, ge=1, le=100),
        cursor: str | None = None,
        project_slug: str | None = None,
        status: str | None = None,
        author: str | None = Query(default=None, max_length=200),
        created_from: str | None = None,
        created_to: str | None = None,
        sort: str = Query(default="created_desc"),
    ):
        if sort not in _SORTS:
            raise PublicError("invalid_sort", "unsupported submission order", 422)
        if status is not None and status not in _STATES:
            raise PublicError("invalid_status", "unsupported submission state", 422, field="status")
        if project_slug is not None:
            try:
                validate_slug(project_slug)
            except InvalidProjectNameError as exc:
                raise PublicError("invalid_project_slug", "invalid project slug", 422, field="project_slug") from exc
        created_from = _utc_filter(created_from, "created_from")
        created_to = _utc_filter(created_to, "created_to")
        if created_from and created_to and created_from > created_to:
            raise PublicError("invalid_date_range", "created_from must not be after created_to", 422)
        actor, roles = actor_roles(request)
        size = limit if limit is not None else page_size
        context = {"v": 2, "kind": "submissions", "actor": actor, "project_slug": project_slug, "status": status, "author": author, "created_from": created_from, "created_to": created_to, "sort": sort}
        last_key = None
        if cursor:
            try:
                decoded = decode_cursor(request.app.state.config.cursor_hmac_secret, cursor)
            except ValueError as exc:
                raise PublicError("invalid_cursor", "invalid cursor", 422) from exc
            if any(decoded.get(key) != value for key, value in context.items()):
                raise PublicError("invalid_cursor", "cursor does not match endpoint, actor, or filters", 422) from None
            now = int(datetime.now(timezone.utc).timestamp())
            if not isinstance(decoded.get("iat"), int) or not isinstance(decoded.get("exp"), int) or decoded["iat"] > now + 60 or decoded["exp"] < now:
                raise PublicError("invalid_cursor", "cursor expired or has an invalid timestamp", 422)
            last_key = decoded.get("last_key")
            if not isinstance(last_key, list) or len(last_key) not in {1, 2}:
                raise PublicError("invalid_cursor", "cursor key is invalid", 422)
        clauses, values = [], []
        if not roles & {"reviewer", "admin"}:
            clauses.append("author_id = ?")
            values.append(actor)
        for field, value, operator in (("project_slug", project_slug, "="), ("state", status, "="), ("author_id", author, "="), ("created_at", created_from, ">="), ("created_at", created_to, "<=")):
            if value:
                clauses.append(f"{field} {operator} ?")
                values.append(value)
        count_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        page_clauses = list(clauses)
        page_values = list(values)
        if last_key is not None:
            direction = ">" if sort.endswith("asc") else "<"
            if sort.startswith("created_"):
                page_clauses.append(f"(created_at {direction} ? OR (created_at = ? AND id {direction} ?))")
                page_values.extend((last_key[0], last_key[0], last_key[1]))
            else:
                page_clauses.append(f"id {direction} ?")
                page_values.append(last_key[0])
        page_where = " WHERE " + " AND ".join(page_clauses) if page_clauses else ""
        connection = connect(saga.database_path)
        try:
            total = int(connection.execute(f"SELECT COUNT(*) FROM submission_records{count_where}", values).fetchone()[0])
            rows = connection.execute(f"SELECT * FROM submission_records{page_where} ORDER BY {_SORTS[sort]} LIMIT ?", (*page_values, size + 1)).fetchall()
        finally:
            connection.close()
        has_more = len(rows) > size
        rows = rows[:size]
        next_cursor = None
        if has_more and rows:
            final = rows[-1]
            key = [final["created_at"], final["id"]] if sort.startswith("created_") else [final["id"]]
            issued = int(datetime.now(timezone.utc).timestamp())
            next_cursor = encode_cursor(request.app.state.config.cursor_hmac_secret, {**context, "iat": issued, "exp": issued + _CURSOR_TTL_SECONDS, "last_key": key})
        return {"items": [_row_dict(row) for row in rows], "total": total, "next_cursor": next_cursor, "page_size": size}

    def load_submission(submission_id: str, request: Request):
        if not _ID_RE.fullmatch(submission_id):
            raise PublicError("invalid_submission_id", "invalid submission id", 422)
        actor, roles = actor_roles(request)
        connection = connect(saga.database_path)
        row = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
        if not row or not can_view(row, actor, roles):
            connection.close()
            raise PublicError("not_found", "submission not found", 404)
        return connection, row

    @router.get("/submissions/{submission_id}")
    def detail(submission_id: str, request: Request):
        connection, row = load_submission(submission_id, request)
        try:
            result = _row_dict(row)
            result["git"] = json.loads(row["git_json"] or "{}")
            result["predecessor"] = None
            if row["predecessor_id"]:
                predecessor = connection.execute("SELECT id,version_label,state FROM submission_records WHERE id=?", (row["predecessor_id"],)).fetchone()
                result["predecessor"] = dict(predecessor) if predecessor else None
            build = connection.execute("SELECT id,provider,status,input_sha256,harness_sha256,error_code,started_at,finished_at FROM submission_builds WHERE submission_id=?", (submission_id,)).fetchone()
            result["build"] = dict(build) if build else None
            result["phases"] = [dict(item) for item in connection.execute("SELECT phase,status,error_code,updated_at FROM submission_phases WHERE submission_id=? ORDER BY CASE phase WHEN 'validation' THEN 1 WHEN 'snapshot' THEN 2 WHEN 'build' THEN 3 WHEN 'package' THEN 4 WHEN 'register' THEN 5 ELSE 6 END", (submission_id,)).fetchall()]
            result["log_phases"] = [str(item["phase"]) for item in connection.execute("SELECT phase FROM submission_log_refs WHERE submission_id=? ORDER BY phase", (submission_id,)).fetchall()]
            actor, roles = actor_roles(request)
            actions = ["submission.download"]
            if roles & {"reviewer", "admin"} and row["state"] in {"submitted", "under_review"}:
                actions.append("submission.review")
            if "admin" in roles and row["state"] == "approved":
                actions.append("submission.publish")
            if "admin" in roles:
                actions.append("submission.effects.retry")
            result["actions_allowed"] = actions
            result["comments"] = [dict(item) for item in connection.execute("SELECT id,seq,author_id AS author,body,created_at FROM submission_comments WHERE submission_id=? ORDER BY seq", (submission_id,)).fetchall()]
            result["events"] = [dict(item) for item in connection.execute("SELECT seq,event_type,actor_id,actor_roles_json,old_state,new_state,data_json,correlation_id,created_at FROM submission_operation_events WHERE submission_id=? ORDER BY seq", (submission_id,)).fetchall()]
            effect_rows = connection.execute(
                "SELECT id,submission_id,kind,provider_key,generation,status,provider_id,last_error_code,lease_owner,lease_until,created_at,updated_at "
                "FROM outbox_effects WHERE submission_id=? ORDER BY created_at",
                (submission_id,),
            ).fetchall()
            result["effects"] = []
            for effect_row in effect_rows:
                effect = {
                    key: effect_row[key]
                    for key in (
                        "id", "kind", "provider_key", "generation", "status", "provider_id",
                        "last_error_code", "created_at", "updated_at",
                    )
                }
                in_flight, retry_after = effect_retry_state(connection, effect_row)
                effect["retry_in_flight"] = in_flight
                if in_flight:
                    effect["retry_after"] = retry_after
                result["effects"].append(effect)
            preview = connection.execute("SELECT id,state,digest,error_code FROM preview_objects WHERE subject_kind='submission' AND subject_id=? ORDER BY created_at DESC LIMIT 1", (submission_id,)).fetchone()
            result["preview"] = dict(preview) if preview else None
            if result["preview"]:
                preview_recoverable = (
                    result["preview"]["state"] in {"failed", "unavailable"}
                    and result["preview"].get("error_code") in {
                        "build_timeout", "build_capacity_exceeded", "project_build_capacity_exceeded", "storage_capacity_exceeded",
                    }
                    and bool(row["snapshot_ref"] and row["input_sha256"] and row["harness_sha256"])
                )
                result["preview"]["recoverable"] = preview_recoverable
                if preview_recoverable and request.app.state.config.is_local and roles & {"reviewer", "admin"}:
                    actions.append("submission.preview.retry")
            return result
        finally:
            connection.close()

    @router.get("/submissions/{submission_id}/manifest")
    def manifest(
        submission_id: str,
        request: Request,
        page_size: int = Query(default=250, ge=1, le=1000),
        cursor: str | None = None,
    ):
        connection, row = load_submission(submission_id, request)
        try:
            if not row["manifest_json"]:
                raise PublicError("artifact_unavailable", "manifest is not available", 409)
            document = json.loads(row["manifest_json"])
            files = document.get("files", [])
            if not isinstance(files, list):
                raise PublicError("artifact_integrity_failed", "manifest files are invalid", 409)
            actor, _roles = actor_roles(request)
            offset = 0
            if cursor:
                try:
                    decoded = decode_cursor(request.app.state.config.cursor_hmac_secret, cursor)
                except ValueError as exc:
                    raise PublicError("invalid_cursor", "invalid manifest cursor", 422) from exc
                now = int(datetime.now(timezone.utc).timestamp())
                if decoded.get("kind") != "manifest" or decoded.get("actor") != actor or decoded.get("submission_id") != submission_id or decoded.get("exp", 0) < now:
                    raise PublicError("invalid_cursor", "manifest cursor does not match this resource", 422)
                offset = decoded.get("offset")
                if not isinstance(offset, int) or offset < 0 or offset > len(files):
                    raise PublicError("invalid_cursor", "manifest cursor offset is invalid", 422)
            page = files[offset:offset + page_size]
            next_cursor = None
            if offset + len(page) < len(files):
                issued = int(datetime.now(timezone.utc).timestamp())
                next_cursor = encode_cursor(request.app.state.config.cursor_hmac_secret, {
                    "kind": "manifest",
                    "actor": actor,
                    "submission_id": submission_id,
                    "offset": offset + len(page),
                    "iat": issued,
                    "exp": issued + _CURSOR_TTL_SECONDS,
                })
            return {
                **{key: value for key, value in document.items() if key != "files"},
                "files": page,
                "total": len(files),
                "page_size": page_size,
                "next_cursor": next_cursor,
            }
        finally:
            connection.close()

    @router.get("/submissions/{submission_id}/logs")
    def logs(submission_id: str, request: Request, phase: str = "frontend", cursor: str | None = None, line_limit: int = Query(default=200, ge=1, le=500)):
        connection, _row = load_submission(submission_id, request)
        log_row = connection.execute(
            "SELECT phase,truncated FROM submission_log_refs WHERE submission_id=? AND phase=?",
            (submission_id, phase),
        ).fetchone()
        connection.close()
        if log_row is None:
            raise PublicError("invalid_log_phase", "log phase is not available", 422)
        actor, _roles = actor_roles(request)
        provider_cursor = None
        if cursor:
            try:
                decoded = decode_cursor(request.app.state.config.cursor_hmac_secret, cursor)
            except ValueError as exc:
                raise PublicError("invalid_cursor", "invalid log cursor", 422) from exc
            now = int(datetime.now(timezone.utc).timestamp())
            if (
                decoded.get("kind") != "log"
                or decoded.get("actor") != actor
                or decoded.get("submission_id") != submission_id
                or decoded.get("phase") != phase
                or decoded.get("exp", 0) < now
                or not isinstance(decoded.get("provider_cursor"), str)
            ):
                raise PublicError("invalid_cursor", "log cursor does not match this resource", 422)
            provider_cursor = decoded["provider_cursor"]
        try:
            page = providers.logs.read_page(submission_id, phase, provider_cursor, line_limit, 262144)
        except ValueError as exc:
            raise PublicError("invalid_cursor", "invalid log cursor", 422) from exc
        next_cursor = None
        if page.next_cursor:
            issued = int(datetime.now(timezone.utc).timestamp())
            next_cursor = encode_cursor(request.app.state.config.cursor_hmac_secret, {
                "kind": "log",
                "actor": actor,
                "submission_id": submission_id,
                "phase": phase,
                "provider_cursor": page.next_cursor,
                "iat": issued,
                "exp": issued + _CURSOR_TTL_SECONDS,
            })
        return {
            "lines": list(page.lines),
            "next_cursor": next_cursor,
            "has_more": page.has_more,
            "truncated": bool(log_row["truncated"]) or page.truncated,
        }

    @router.get("/submissions/{submission_id}/artifact")
    def artifact(submission_id: str, request: Request):
        connection, row = load_submission(submission_id, request)
        try:
            if row["state"] not in {"submitted", "under_review", "changes_requested", "rejected", "approved", "published"} or not row["artifact_ref"]:
                raise PublicError("artifact_unavailable", "artifact is not available", 409)
            if row["artifact_provider"] == "legacy":
                source = connection.execute("SELECT * FROM legacy_sources WHERE submission_id=?", (submission_id,)).fetchone()
                if source is None:
                    raise PublicError("artifact_unavailable", "legacy source is not available", 409)
                legacy = LegacyArtifactStore(Path(__file__).resolve().parents[3] / "submissions")
                opened = legacy.open_source(
                    source["relative_path"], file_id=source["file_id"], size=source["size"],
                    mtime_ns=source["mtime_ns"], sha256=source["sha256"],
                )
                try:
                    handle = opened.__enter__()
                except (OSError, ValueError) as exc:
                    connection.execute("UPDATE legacy_sources SET state='quarantined' WHERE id=? AND state='indexed'", (source["id"],))
                    connection.commit()
                    raise PublicError("artifact_integrity_failed", "legacy artifact integrity check failed", 409) from exc

                def legacy_content():
                    try:
                        while chunk := handle.read(1024 * 1024):
                            yield chunk
                    finally:
                        opened.__exit__(None, None, None)
                headers = {"Content-Length": str(source["size"]), "Digest": "sha-256=" + base64.b64encode(bytes.fromhex(source["sha256"])).decode(), "Content-Disposition": f'attachment; filename="{Path(source["relative_path"]).name}"', "X-Content-Type-Options": "nosniff"}
                return StreamingResponse(legacy_content(), media_type="application/zip", headers=headers)
            ref = ObjectRef(str(row["artifact_provider"]), row["artifact_ref"])
            open_verified = getattr(providers.artifacts, "open_verified", None)
            if callable(open_verified):
                opened = open_verified(ref, size=int(row["artifact_size"]), sha256=str(row["artifact_sha256"]))
                try:
                    handle, stat_result = opened.__enter__()
                except (OSError, ValueError) as exc:
                    raise PublicError("artifact_integrity_failed", "artifact integrity check failed", 409) from exc

                def verified_content():
                    try:
                        while chunk := handle.read(1024 * 1024):
                            yield chunk
                    finally:
                        opened.__exit__(None, None, None)
                headers = {"Content-Length": str(stat_result.size), "Digest": "sha-256=" + base64.b64encode(bytes.fromhex(stat_result.sha256)).decode(), "Content-Disposition": f'attachment; filename="{row["project_slug"]}-{row["version_label"]}.zip"', "X-Content-Type-Options": "nosniff"}
                return StreamingResponse(verified_content(), media_type="application/zip", headers=headers)
            stat_result = providers.artifacts.stat(ref)
            if stat_result.size != row["artifact_size"] or stat_result.sha256 != row["artifact_sha256"]:
                raise PublicError("artifact_integrity_failed", "artifact integrity check failed", 409)
            spec = providers.artifacts.create_download(ref, f"{row['project_slug']}-{row['version_label']}.zip", 900)
            if spec.kind == "redirect":
                return RedirectResponse(spec.url, status_code=303)

            def content():
                with providers.artifacts.open_stream(ref) as handle:
                    while chunk := handle.read(1024 * 1024):
                        yield chunk
            headers = {"Content-Length": str(stat_result.size), "Digest": "sha-256=" + base64.b64encode(bytes.fromhex(stat_result.sha256)).decode(), "Content-Disposition": f'attachment; filename="{row["project_slug"]}-{row["version_label"]}.zip"', "X-Content-Type-Options": "nosniff"}
            return StreamingResponse(content(), media_type="application/zip", headers=headers)
        finally:
            connection.close()

    return router
