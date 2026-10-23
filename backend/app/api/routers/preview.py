from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Header, Request

from app.api.routers.review import actor_roles
from app.api.schemas import PreviewGrantCreate
from app.auth.authorization import require_workspace
from app.core.errors import PublicError
from app.projects import registry
from app.projects.workspace_recovery import workspace_lock
from app.submissions.build import BuildFailed, LocalSubprocessBuildProvider
from app.submissions.database import connect, transaction
from app.submissions.idempotency import validate_idempotency_key
from app.submissions.preview import PreviewManager, load_sealed_preview_tree, preview_tree_manifest_json
from app.submissions.providers import BuildRequest
from app.submissions.repository_v2 import PipelineRepository, now_iso
from app.submissions.runtime_refs import encode_runtime_ref, resolve_runtime_ref
from app.submissions.review import require_visible
from app.submissions.snapshot import SafeSnapshotBuilder, load_sealed_snapshot, snapshot_manifest_json

_PREVIEW_OPERATION_ID_RE = re.compile(r"^pop_[0-9a-f]{32}$")
_SUBMISSION_ID_RE = re.compile(r"^sub_[0-9a-f]{32}$")
_PREVIEW_ID_RE = re.compile(r"^prv_[0-9a-f]{32}$")


def _require_public_id(value: str, pattern: re.Pattern[str], code: str, label: str) -> str:
    if not pattern.fullmatch(value):
        raise PublicError(code, f"invalid {label}", 422)
    return value


_RECOVERABLE_PREVIEW_ERRORS = {
    "workspace_recovering",
    "build_capacity_exceeded",
    "project_build_capacity_exceeded",
    "build_timeout",
    "storage_capacity_exceeded",
}


class WorkspacePreviewBuildService:
    def __init__(self, database_path: Path, runtime_root: Path, config, workspace_guard=None, build_provider=None) -> None:
        self.database_path = Path(database_path)
        self.runtime_root = Path(runtime_root)
        self.config = config
        self.workspace_guard = workspace_guard
        self.build_provider = build_provider or LocalSubprocessBuildProvider(
            registry.FRONTEND_PROJECTS_DIR.parents[1],
            max_log_bytes=config.log_phase_max_bytes,
            max_builds_global=config.max_builds_global,
            max_builds_per_project=config.max_builds_per_project,
        )

    def _response(self, row) -> dict[str, object]:
        phase_order = ("reserved", "snapshot", "build", "seal", "ready")
        current_phase = str(row["phase"])
        current_index = phase_order.index(current_phase) if current_phase in phase_order else 0
        phases = []
        for index, phase in enumerate(phase_order):
            if row["state"] == "ready" or index < current_index:
                status = "succeeded"
            elif index == current_index:
                status = "failed" if row["state"] == "failed" else "running" if row["state"] == "running" else "pending"
            else:
                status = "pending"
            phases.append({"phase": phase, "status": status, "error_code": row["error_code"] if status == "failed" else None})
        payload: dict[str, object] = {
            "operation_id": row["operation_id"],
            "state": row["state"],
            "status": row["state"],
            "phase": row["phase"],
            "poll_url": f"/api/preview-operations/{row['operation_id']}",
            "phases": phases,
            "recoverable": bool(row["recoverable"]),
            "subject_kind": row["subject_kind"],
            "subject_id": row["subject_id"] or row["project_slug"],
            "timeout_seconds": self.config.build_timeout_seconds,
            "created_at": row["created_at"],
        }
        if row["preview_id"]:
            payload["preview_id"] = row["preview_id"]
            connection = connect(self.database_path)
            try:
                preview = connection.execute("SELECT digest FROM preview_objects WHERE id=?", (row["preview_id"],)).fetchone()
                if preview:
                    payload["digest"] = preview["digest"]
            finally:
                connection.close()
        if row["error_code"]:
            payload["error_code"] = row["error_code"]
        return payload

    def get(self, operation_id: str):
        connection = connect(self.database_path)
        try:
            return connection.execute("SELECT * FROM preview_build_operations WHERE operation_id=?", (operation_id,)).fetchone()
        finally:
            connection.close()

    def _reserve(
        self,
        *,
        slug: str,
        actor: str,
        idempotency_key: str,
        request: dict[str, object],
        subject_kind: str,
        subject_id: str,
        source_preview_id: str | None = None,
        input_sha256: str | None = None,
        harness_sha256: str | None = None,
        snapshot_ref: str | None = None,
        snapshot_manifest_json: str | None = None,
        output_ref: str | None = None,
        output_manifest_json: str | None = None,
        correlation_id: str | None = None,
        reason: str | None = None,
        actor_roles: set[str] | None = None,
    ) -> tuple[dict[str, object], bool]:
        validate_idempotency_key(idempotency_key)
        request_sha256 = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                existing = connection.execute(
                    "SELECT * FROM preview_build_operations WHERE actor_id=? AND project_slug=? AND idempotency_key=?",
                    (actor, slug, idempotency_key),
                ).fetchone()
                if existing:
                    if existing["request_sha256"] != request_sha256:
                        raise PublicError("idempotency_conflict", "Idempotency-Key was already used with different input", 409)
                    return self._response(existing), False
                if subject_kind == "submission":
                    active_subject = connection.execute(
                        "SELECT * FROM preview_build_operations WHERE subject_kind='submission' AND subject_id=? "
                        "AND input_sha256=? AND harness_sha256=? AND state IN ('queued','running') ORDER BY created_at LIMIT 1",
                        (subject_id, input_sha256, harness_sha256),
                    ).fetchone()
                else:
                    active_subject = connection.execute(
                        "SELECT * FROM preview_build_operations WHERE subject_kind='workspace' AND project_slug=? "
                        "AND state IN ('queued','running') ORDER BY created_at LIMIT 1",
                        (slug,),
                    ).fetchone()
                if active_subject is not None:
                    return self._response(active_subject), False
                active = int(connection.execute("SELECT COUNT(*) FROM preview_build_operations WHERE state IN ('queued','running')").fetchone()[0])
                active_project = int(connection.execute(
                    "SELECT COUNT(*) FROM preview_build_operations WHERE project_slug=? AND state IN ('queued','running')",
                    (slug,),
                ).fetchone()[0])
                reserve_bytes = self.config.snapshot_max_bytes + self.config.artifact_max_bytes
                held = int(connection.execute("SELECT COALESCE(SUM(bytes),0) FROM storage_reservations WHERE state='held'").fetchone()[0])
                if active >= self.config.max_builds_global:
                    raise PublicError("build_capacity_exceeded", "preview build capacity is exhausted", 429, recoverable=True)
                if active_project >= self.config.max_builds_per_project:
                    raise PublicError("project_build_capacity_exceeded", "project preview build capacity is exhausted", 429, recoverable=True)
                self.runtime_root.mkdir(parents=True, exist_ok=True)
                if held + reserve_bytes > self.config.storage_spool_max_bytes or shutil.disk_usage(self.runtime_root).free < self.config.storage_min_free_bytes + reserve_bytes:
                    raise PublicError("storage_capacity_exceeded", "storage capacity is exhausted", 507, recoverable=True)
                operation_id = "pop_" + secrets.token_hex(16)
                reservation_id = "res_" + secrets.token_hex(16)
                now = now_iso()
                expires = (datetime.now(timezone.utc) + timedelta(seconds=self.config.build_timeout_seconds + 60)).isoformat()
                connection.execute(
                    "INSERT INTO storage_reservations(id,kind,owner_id,bytes,state,expires_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (reservation_id, "preview", operation_id, reserve_bytes, "held", expires, now),
                )
                connection.execute(
                    "INSERT INTO preview_build_operations(operation_id,actor_id,project_slug,idempotency_key,request_sha256,state,phase,reservation_id,created_at,updated_at,subject_kind,subject_id,source_preview_id,input_sha256,harness_sha256,snapshot_ref,snapshot_manifest_json,output_ref,output_manifest_json,correlation_id,reason) "
                    "VALUES (?,?,?,?,?,'queued','reserved',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id, actor, slug, idempotency_key, request_sha256, reservation_id, now, now,
                        subject_kind, subject_id, source_preview_id, input_sha256, harness_sha256, snapshot_ref,
                        snapshot_manifest_json, output_ref, output_manifest_json, correlation_id, reason,
                    ),
                )
                if subject_kind == "submission":
                    PipelineRepository(connection).append_event(
                        subject_id,
                        "preview_retry_reserved",
                        actor,
                        {"operation_id": operation_id, "source_preview_id": source_preview_id, "reason": reason},
                        actor_roles=tuple(sorted(actor_roles or set())),
                        correlation_id=correlation_id,
                    )
                row = connection.execute("SELECT * FROM preview_build_operations WHERE operation_id=?", (operation_id,)).fetchone()
                return self._response(row), True
        finally:
            connection.close()

    def reserve(self, slug: str, actor: str, idempotency_key: str) -> tuple[dict[str, object], bool]:
        return self._reserve(
            slug=slug,
            actor=actor,
            idempotency_key=idempotency_key,
            request={"project_slug": slug, "subject_kind": "workspace"},
            subject_kind="workspace",
            subject_id=slug,
        )

    def reserve_submission(
        self,
        submission_id: str,
        actor: str,
        roles: set[str],
        idempotency_key: str,
        correlation_id: str | None,
    ) -> tuple[dict[str, object], bool]:
        _require_public_id(submission_id, _SUBMISSION_ID_RE, "invalid_submission_id", "submission id")
        connection = connect(self.database_path)
        try:
            submission = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
            require_visible(submission, actor, roles)
            if not roles & {"reviewer", "admin"}:
                raise PublicError("forbidden", "submission.preview.retry capability required", 403)
            replay = connection.execute(
                "SELECT * FROM preview_build_operations WHERE actor_id=? AND project_slug=? AND idempotency_key=?",
                (actor, submission["project_slug"], idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["subject_kind"] != "submission" or replay["subject_id"] != submission_id:
                    raise PublicError("idempotency_conflict", "Idempotency-Key was already used with different input", 409)
                return self._response(replay), False
            preview = connection.execute(
                "SELECT * FROM preview_objects WHERE subject_kind='submission' AND subject_id=? ORDER BY created_at DESC LIMIT 1",
                (submission_id,),
            ).fetchone()
            if preview is None:
                raise PublicError("preview_retry_unavailable", "submission preview has no recoverable failure", 409)
            error_code = str(preview["error_code"] or "")
            if preview["state"] not in {"failed", "unavailable"} or error_code not in _RECOVERABLE_PREVIEW_ERRORS:
                raise PublicError("preview_retry_not_recoverable", "submission preview failure is not recoverable", 409)
            if not submission["snapshot_ref"] or not submission["snapshot_manifest_json"] or not submission["input_sha256"] or not submission["harness_sha256"]:
                raise PublicError("preview_retry_unavailable", "sealed submission input is unavailable", 409)
            request = {
                "project_slug": submission["project_slug"],
                "subject_kind": "submission",
                "subject_id": submission_id,
                "source_preview_id": preview["id"],
                "source_digest": preview["digest"],
                "input_sha256": submission["input_sha256"],
                "harness_sha256": submission["harness_sha256"],
            }
            return self._reserve(
                slug=str(submission["project_slug"]),
                actor=actor,
                idempotency_key=idempotency_key,
                request=request,
                subject_kind="submission",
                subject_id=submission_id,
                source_preview_id=str(preview["id"]),
                input_sha256=str(submission["input_sha256"]),
                harness_sha256=str(submission["harness_sha256"]),
                snapshot_ref=encode_runtime_ref(self.runtime_root, resolve_runtime_ref(self.runtime_root, str(submission["snapshot_ref"]))),
                snapshot_manifest_json=str(submission["snapshot_manifest_json"]),
                correlation_id=correlation_id,
                reason=f"recoverable_preview_error:{error_code}",
                actor_roles=roles,
            )
        finally:
            connection.close()

    def start(self, operation_id: str, *, recover: bool = False) -> None:
        thread = threading.Thread(target=self.run, args=(operation_id,), kwargs={"recover": recover}, name=f"preview-{operation_id[-8:]}", daemon=True)
        thread.start()

    def recover(self) -> list[str]:
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT operation_id FROM preview_build_operations "
                "WHERE state='queued' OR (state='running' AND (lease_until IS NULL OR lease_until<=?)) ORDER BY created_at",
                (datetime.now(timezone.utc).isoformat(),),
            ).fetchall()
        finally:
            connection.close()
        operation_ids = [str(row["operation_id"]) for row in rows]
        for operation_id in operation_ids:
            self.start(operation_id, recover=True)
        return operation_ids

    def _claim(self, operation_id: str, worker: str, recover: bool) -> tuple[object, int] | tuple[None, None]:
        del recover
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                now = datetime.now(timezone.utc)
                row = connection.execute("SELECT * FROM preview_build_operations WHERE operation_id=?", (operation_id,)).fetchone()
                if row is None or row["state"] in {"ready", "failed"}:
                    return None, None
                if row["state"] == "running" and row["lease_until"] and datetime.fromisoformat(row["lease_until"]) > now:
                    return None, None
                generation = int(row["lease_generation"]) + 1
                lease_until = (now + timedelta(seconds=self.config.build_timeout_seconds + 60)).isoformat()
                cursor = connection.execute(
                    "UPDATE preview_build_operations SET state='running',lease_owner=?,lease_generation=?,lease_until=?,updated_at=? "
                    "WHERE operation_id=? AND lease_generation=? AND "
                    "(state='queued' OR (state='running' AND (lease_until IS NULL OR lease_until<=?)))",
                    (worker, generation, lease_until, now_iso(), operation_id, row["lease_generation"], now.isoformat()),
                )
                if cursor.rowcount != 1:
                    return None, None
                return connection.execute("SELECT * FROM preview_build_operations WHERE operation_id=?", (operation_id,)).fetchone(), generation
        finally:
            connection.close()

    def _update(self, operation_id: str, worker: str, generation: int, **values) -> None:
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                assignments = ",".join(f"{name}=?" for name in values)
                cursor = connection.execute(
                    f"UPDATE preview_build_operations SET {assignments},updated_at=? WHERE operation_id=? AND lease_owner=? AND lease_generation=? AND state='running'",
                    (*values.values(), now_iso(), operation_id, worker, generation),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("preview build lease was lost")
        finally:
            connection.close()

    def run(self, operation_id: str, *, recover: bool = False) -> None:
        worker = "preview-worker-" + uuid.uuid4().hex
        row, generation = self._claim(operation_id, worker, recover)
        if row is None or generation is None:
            return
        try:
            slug = str(row["project_slug"])
            subject_kind = str(row["subject_kind"])
            subject_id = str(row["subject_id"] or slug)
            if subject_kind == "workspace" and self.workspace_guard is not None:
                self.workspace_guard(self.database_path, slug)
            snapshot_root = resolve_runtime_ref(self.runtime_root, str(row["snapshot_ref"])) if row["snapshot_ref"] else None
            snapshot_manifest = str(row["snapshot_manifest_json"] or "")
            input_sha256 = str(row["input_sha256"] or "")
            if subject_kind == "workspace" and snapshot_root is None and not snapshot_manifest and not input_sha256:
                self._update(operation_id, worker, generation, phase="snapshot")
                with workspace_lock(self.database_path, slug):
                    if self.workspace_guard is not None:
                        self.workspace_guard(self.database_path, slug)
                    snapshot = SafeSnapshotBuilder(
                        registry.FRONTEND_PROJECTS_DIR,
                        registry.BACKEND_PROJECTS_DIR,
                        max_files=self.config.snapshot_max_files,
                        max_bytes=self.config.snapshot_max_bytes,
                    ).build(slug, self.runtime_root / "preview-inputs" / f"{operation_id}-g{generation}")
                snapshot_root = snapshot.root
                input_sha256 = snapshot.input_sha256
                snapshot_manifest = snapshot_manifest_json(snapshot)
                self._update(
                    operation_id,
                    worker,
                    generation,
                    snapshot_ref=encode_runtime_ref(self.runtime_root, snapshot_root),
                    snapshot_manifest_json=snapshot_manifest,
                    input_sha256=input_sha256,
                )
            if snapshot_root is None or not snapshot_manifest or not input_sha256:
                raise PublicError("preview_integrity_failed", "sealed preview input metadata is incomplete", 409)
            try:
                snapshot = load_sealed_snapshot(
                    snapshot_root,
                    slug,
                    snapshot_manifest,
                    input_sha256,
                    max_files=self.config.snapshot_max_files,
                    max_bytes=self.config.snapshot_max_bytes,
                )
            except (OSError, ValueError) as exc:
                raise PublicError("preview_integrity_failed", "sealed preview input changed", 409) from exc
            snapshot_root = snapshot.root

            output_root = resolve_runtime_ref(self.runtime_root, str(row["output_ref"])) if row["output_ref"] else None
            output_manifest = str(row["output_manifest_json"] or "")
            harness_sha256 = str(row["harness_sha256"] or "")
            if output_root is None:
                if output_manifest:
                    raise PublicError("preview_integrity_failed", "sealed preview output metadata is incomplete", 409)
                self._update(operation_id, worker, generation, phase="build")
                output_root = self.runtime_root / "preview-builds" / f"{operation_id}-g{generation}"
                build = self.build_provider.execute(
                    BuildRequest(slug, snapshot_root, output_root, timeout_seconds=self.config.build_timeout_seconds, input_sha256=input_sha256)
                )
                if not build.ok:
                    raise BuildFailed(build.error_code or "build_failed")
                harness_sha256 = build.harness_sha256
                output_manifest = preview_tree_manifest_json(
                    output_root / "preview",
                    max_files=self.config.snapshot_max_files,
                    max_bytes=self.config.artifact_max_bytes,
                )
                self._update(
                    operation_id,
                    worker,
                    generation,
                    output_ref=encode_runtime_ref(self.runtime_root, output_root),
                    output_manifest_json=output_manifest,
                    harness_sha256=harness_sha256,
                )
            if not harness_sha256 or not output_manifest:
                raise PublicError("preview_integrity_failed", "sealed preview output metadata is incomplete", 409)
            try:
                load_sealed_preview_tree(
                    output_root / "preview",
                    output_manifest,
                    max_files=self.config.snapshot_max_files,
                    max_bytes=self.config.artifact_max_bytes,
                )
            except (OSError, ValueError) as exc:
                raise PublicError("preview_integrity_failed", "sealed preview output changed", 409) from exc
            self._update(operation_id, worker, generation, phase="seal")
            preview = PreviewManager(self.database_path, self.runtime_root / "previews", preview_origin=self.config.preview_origin).seal(
                subject_kind=subject_kind,
                subject_id=subject_id,
                source=output_root / "preview",
                input_sha256=input_sha256,
                harness_sha256=harness_sha256,
                runtime_root=snapshot_root / "backend",
                project_slug=slug,
            )
            connection = connect(self.database_path)
            try:
                with transaction(connection):
                    cursor = connection.execute(
                        "UPDATE preview_build_operations SET state='ready',phase='ready',preview_id=?,error_code=NULL,recoverable=0,lease_until=NULL,updated_at=? "
                        "WHERE operation_id=? AND lease_owner=? AND lease_generation=? AND state='running'",
                        (preview["id"], now_iso(), operation_id, worker, generation),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("preview build lease was lost")
                    connection.execute("UPDATE storage_reservations SET state='committed',updated_at=? WHERE id=? AND state='held'", (now_iso(), row["reservation_id"]))
                    if subject_kind == "submission":
                        PipelineRepository(connection).append_event(
                            subject_id,
                            "preview_retry_succeeded",
                            str(row["actor_id"]),
                            {"operation_id": operation_id, "preview_id": preview["id"], "reason": row["reason"]},
                            correlation_id=row["correlation_id"],
                        )
            finally:
                connection.close()
        except Exception as exc:
            code = exc.code if isinstance(exc, (BuildFailed, PublicError)) else "preview_build_internal_error"
            recoverable = code in _RECOVERABLE_PREVIEW_ERRORS
            connection = connect(self.database_path)
            try:
                with transaction(connection):
                    connection.execute(
                        "UPDATE preview_build_operations SET state='failed',error_code=?,recoverable=?,lease_until=NULL,updated_at=? "
                        "WHERE operation_id=? AND lease_owner=? AND lease_generation=? AND state='running'",
                        (code, int(recoverable), now_iso(), operation_id, worker, generation),
                    )
                    connection.execute("UPDATE storage_reservations SET state='released',updated_at=? WHERE id=? AND state='held'", (now_iso(), row["reservation_id"]))
                    if row["subject_kind"] == "submission":
                        PipelineRepository(connection).append_event(
                            str(row["subject_id"]),
                            "preview_retry_failed",
                            str(row["actor_id"]),
                            {"operation_id": operation_id, "error_code": code, "reason": row["reason"]},
                            correlation_id=row["correlation_id"],
                        )
            finally:
                connection.close()


def create_preview_router(database_path, runtime_root, config=None, *, workspace_guard=None, build_provider=None) -> APIRouter:
    router = APIRouter(prefix="/api")
    service = WorkspacePreviewBuildService(database_path, runtime_root, config, workspace_guard, build_provider)
    router.preview_build_service = service

    def preview_manager(request: Request) -> PreviewManager:
        return PreviewManager(database_path, runtime_root / "previews", preview_origin=request.app.state.config.preview_origin)

    @router.post("/projects/{slug}/preview-builds", status_code=202)
    def build_workspace_preview(slug: str, request: Request, idempotency_key: str = Header(alias="Idempotency-Key")):
        actor, roles = actor_roles(request)
        if not request.app.state.config.is_local:
            raise PublicError("feature_unavailable", "workspace preview is disabled in shared profile; use an isolated submission build", 404)
        if not registry.project_exists(slug):
            raise PublicError("not_found", "project not found", 404)
        connection = connect(database_path)
        try:
            require_workspace(connection, slug, actor, roles, "project.preview")
        finally:
            connection.close()
        if workspace_guard is not None:
            workspace_guard(database_path, slug)
        response, created = service.reserve(slug, actor, idempotency_key)
        if created:
            service.start(str(response["operation_id"]))
        return response

    @router.post("/submissions/{submission_id}/preview-retries", status_code=202)
    def retry_submission_preview(submission_id: str, request: Request, idempotency_key: str = Header(alias="Idempotency-Key")):
        if not request.app.state.config.is_local:
            raise PublicError("feature_unavailable", "submission preview rebuild is unavailable without an isolated preview build provider", 404)
        actor, roles = actor_roles(request)
        response, created = service.reserve_submission(
            submission_id,
            actor,
            roles,
            idempotency_key,
            str(getattr(request.state, "correlation_id", "")),
        )
        if created:
            service.start(str(response["operation_id"]))
        return response

    @router.get("/preview-operations/{operation_id}")
    def preview_operation(operation_id: str, request: Request):
        _require_public_id(operation_id, _PREVIEW_OPERATION_ID_RE, "invalid_preview_operation_id", "preview operation id")
        actor, roles = actor_roles(request)
        row = service.get(operation_id)
        if row is None:
            raise PublicError("not_found", "preview operation not found", 404)
        connection = connect(database_path)
        try:
            if row["subject_kind"] == "submission":
                submission = connection.execute("SELECT * FROM submission_records WHERE id=?", (row["subject_id"],)).fetchone()
                require_visible(submission, actor, roles)
            else:
                require_workspace(connection, str(row["project_slug"]), actor, roles, "project.preview")
        finally:
            connection.close()
        if row["actor_id"] != actor and "admin" not in roles:
            raise PublicError("not_found", "preview operation not found", 404)
        return service._response(row)

    @router.get("/preview-status/{preview_id}")
    def preview_status(preview_id: str, request: Request):
        _require_public_id(preview_id, _PREVIEW_ID_RE, "invalid_preview_id", "preview id")
        actor, roles = actor_roles(request)
        connection = connect(database_path)
        try:
            row = connection.execute("SELECT * FROM preview_objects WHERE id=?", (preview_id,)).fetchone()
            if row is None:
                raise PublicError("not_found", "preview not found", 404)
            if row["subject_kind"] == "submission":
                submission = connection.execute("SELECT * FROM submission_records WHERE id=?", (row["subject_id"],)).fetchone()
                require_visible(submission, actor, roles)
            else:
                if not registry.project_exists(str(row["subject_id"])):
                    raise PublicError("not_found", "project not found", 404)
                require_workspace(connection, str(row["subject_id"]), actor, roles, "project.preview")
            current = connection.execute(
                "SELECT id FROM preview_objects WHERE subject_kind=? AND subject_id=? AND state='ready' "
                "ORDER BY updated_at DESC,id DESC LIMIT 1",
                (row["subject_kind"], row["subject_id"]),
            ).fetchone()
            current_preview_id = str(current["id"]) if current is not None else None
            return {
                "preview_id": preview_id,
                "state": str(row["state"]),
                "error_code": row["error_code"],
                "current": preview_id == current_preview_id,
                "current_preview_id": current_preview_id,
            }
        finally:
            connection.close()

    @router.post("/preview-grants", status_code=201)
    def preview_grant(body: PreviewGrantCreate, request: Request):
        actor, roles = actor_roles(request)
        connection = connect(database_path)
        try:
            if body.subject_kind == "submission":
                submission = connection.execute("SELECT * FROM submission_records WHERE id=?", (body.subject_id,)).fetchone()
                require_visible(submission, actor, roles)
            elif body.subject_kind == "workspace":
                if not registry.project_exists(body.subject_id):
                    raise PublicError("not_found", "project not found", 404)
                require_workspace(connection, body.subject_id, actor, roles, "project.preview")
            else:
                raise PublicError("invalid_preview_subject", "invalid preview subject", 422)
            row = connection.execute(
                "SELECT * FROM preview_objects WHERE subject_kind=? AND subject_id=? AND state='ready' ORDER BY updated_at DESC,id DESC LIMIT 1" if body.preview_id is None else "SELECT * FROM preview_objects WHERE subject_kind=? AND subject_id=? AND id=?",
                (body.subject_kind, body.subject_id) if body.preview_id is None else (body.subject_kind, body.subject_id, body.preview_id),
            ).fetchone()
            if row is None:
                raise PublicError("not_found", "preview not found", 404)
            preview_id = row["id"]
        finally:
            connection.close()
        return preview_manager(request).create_grant(preview_id, subject_kind=body.subject_kind, subject_id=body.subject_id, actor=actor)

    return router
