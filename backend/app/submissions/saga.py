from __future__ import annotations

import json
import secrets
import shutil
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.errors import PublicError
from app.projects import registry
from app.projects.workspace_recovery import require_workspace_ready, workspace_lock
from app.submissions.build import BuildFailed
from app.submissions.database import connect, transaction
from app.submissions.idempotency import canonical_request_hash, complete, reserve
from app.submissions.jobs import LeaseLost, claim_job, finish_job, heartbeat_job, require_job_lease
from app.submissions.outbox import OutboxDispatcher, enqueue_notification, notification_channels, reconcile_notification_phase
from app.submissions.packaging import create_reproducible_package
from app.submissions.providers import BuildHandle, BuildRequest
from app.submissions.preview import PreviewManager
from app.submissions.repository_v2 import PipelineRepository, now_iso
from app.submissions.runtime_refs import encode_runtime_ref, resolve_runtime_ref
from app.submissions.snapshot import SafeSnapshotBuilder, load_sealed_snapshot, snapshot_manifest_json

_PHASES = ("validation", "snapshot", "build", "package", "register", "notify")


def build_operation_response(connection, operation_id: str, row) -> dict[str, object]:
    if row is None:
        return {"operation_id": operation_id, "status": "unknown"}
    phases = [dict(item) for item in connection.execute(
        "SELECT phase,status,error_code,updated_at FROM submission_phases WHERE submission_id=? "
        "ORDER BY CASE phase WHEN 'validation' THEN 1 WHEN 'snapshot' THEN 2 WHEN 'build' THEN 3 WHEN 'package' THEN 4 WHEN 'register' THEN 5 ELSE 6 END",
        (row["id"],),
    ).fetchall()]
    notifications = [
        {
            "provider_key": item["provider_key"],
            "status": item["status"],
            "error_code": item["last_error_code"],
        }
        for item in connection.execute(
            "SELECT provider_key,status,last_error_code FROM outbox_effects "
            "WHERE submission_id=? AND kind='notification' ORDER BY created_at,provider_key",
            (row["id"],),
        ).fetchall()
    ]
    response: dict[str, object] = {
        "operation_id": operation_id,
        "submission_id": row["id"],
        "status": row["state"],
        "version": row["version_seq"],
        "poll_url": f"/api/submission-operations/{operation_id}",
        "phases": phases,
        "notifications": notifications,
    }
    if row["state"] == "submitted":
        response.update({
            "package_path": f"/api/submissions/{row['id']}/artifact",
            "artifact_sha256": row["artifact_sha256"],
            "notified": False,
        })
    if row["error_code"]:
        response["error"] = row["error_code"]
        if row["error_code"] in {"sealed_snapshot_unavailable", "sealed_snapshot_invalid"}:
            response["recoverable"] = True
    route = connection.execute("SELECT route_template FROM idempotency_records WHERE operation_id=?", (operation_id,)).fetchone()
    if route and route["route_template"] == "/api/projects/{slug}/submit":
        try:
            meta = registry.read_meta(str(row["project_slug"]))
        except (OSError, ValueError):
            meta = {"name": row["project_slug"]}
        response.update({
            "package_path": response.get("package_path"),
            "notified": {"email": False, "slack": False},
            "meta": meta,
            "submission": {
                "id": row["id"],
                "submission_id": row["id"],
                "project_slug": row["project_slug"],
                "version": row["version_seq"],
                "version_label": row["version_label"],
                "state": row["state"],
                "poll_url": response["poll_url"],
            },
        })
        if row["state"] == "build_failed":
            response["error_code"] = row["error_code"] or "build_failed"
            response["error"] = "package_failed"
    return response


def operation_success_status(connection, operation_id: str) -> int:
    route = connection.execute("SELECT route_template FROM idempotency_records WHERE operation_id=?", (operation_id,)).fetchone()
    return 200 if route and route["route_template"] == "/api/projects/{slug}/submit" else 201


class SubmissionSaga:
    def __init__(
        self,
        database_path: Path,
        runtime_root: Path,
        providers,
        *,
        frontend_root: Path | None = None,
        review_base_url: str | None = None,
        preview_origin: str = "http://127.0.0.1:5001",
        lease_seconds: int = 60,
        heartbeat_interval: float = 20.0,
        git_require_clean: bool = False,
        file_exclusions: tuple[str, ...] = (),
        submit_wait_seconds: int = 900,
        build_timeout_seconds: int = 900,
        snapshot_max_files: int = 10_000,
        snapshot_max_bytes: int = 100 * 1024 * 1024,
        artifact_max_bytes: int = 100 * 1024 * 1024,
        max_builds_global: int = 2,
        max_builds_per_project: int = 1,
        storage_spool_max_bytes: int = 2 * 1024 * 1024 * 1024,
        storage_min_free_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self.database_path = Path(database_path)
        self.runtime_root = Path(runtime_root)
        self.providers = providers
        self.review_base_url = str(review_base_url or providers.review_base_url).rstrip("/")
        self.preview_origin = preview_origin.rstrip("/")
        self.frontend_root = Path(frontend_root or registry.FRONTEND_PROJECTS_DIR.parents[1])
        self.lease_seconds = max(int(lease_seconds), 1)
        self.heartbeat_interval = max(float(heartbeat_interval), 0.05)
        self.git_require_clean = bool(git_require_clean)
        self.file_exclusions = tuple(file_exclusions)
        self.submit_wait_seconds = min(max(int(submit_wait_seconds), 1), 3600)
        self.build_timeout_seconds = min(max(int(build_timeout_seconds), 1), 3600)
        self.snapshot_max_files = max(int(snapshot_max_files), 1)
        self.snapshot_max_bytes = max(int(snapshot_max_bytes), 1)
        self.artifact_max_bytes = max(int(artifact_max_bytes), 1)
        self.max_builds_global = max(int(max_builds_global), 1)
        self.max_builds_per_project = max(int(max_builds_per_project), 1)
        self.storage_spool_max_bytes = max(int(storage_spool_max_bytes), 1)
        self.storage_min_free_bytes = max(int(storage_min_free_bytes), 1)

    @staticmethod
    def _set_phase(connection, submission_id: str, phase: str, status: str, error_code: str | None = None) -> None:
        connection.execute(
            "INSERT INTO submission_phases(submission_id,phase,status,error_code,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(submission_id,phase) DO UPDATE SET status=excluded.status,error_code=excluded.error_code,updated_at=excluded.updated_at",
            (submission_id, phase, status, error_code, now_iso()),
        )

    @staticmethod
    def _phase_rows(connection, submission_id: str) -> list[dict[str, object]]:
        rows = connection.execute("SELECT phase,status,error_code,updated_at FROM submission_phases WHERE submission_id=? ORDER BY CASE phase WHEN 'validation' THEN 1 WHEN 'snapshot' THEN 2 WHEN 'build' THEN 3 WHEN 'package' THEN 4 WHEN 'register' THEN 5 ELSE 6 END", (submission_id,)).fetchall()
        return [dict(row) for row in rows]

    def _persist_logs(self, submission_id: str, phases: tuple[dict[str, object], ...]) -> None:
        persisted: dict[str, bool] = {}
        for phase in phases:
            phase_name = str(phase.get("phase", "build"))
            content = str(phase.get("stdout", "")) + str(phase.get("stderr", ""))
            append_result = self.providers.logs.append(submission_id, phase_name, content)
            persisted[phase_name] = (
                persisted.get(phase_name, False)
                or bool(phase.get("truncated"))
                or append_result.truncated
            )
        if not persisted:
            return
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                for phase_name, truncated in persisted.items():
                    connection.execute(
                        "INSERT INTO submission_log_refs(submission_id,phase,log_ref,truncated) VALUES (?,?,?,?) "
                        "ON CONFLICT(submission_id,phase) DO UPDATE SET log_ref=excluded.log_ref,"
                        "truncated=MAX(submission_log_refs.truncated,excluded.truncated)",
                        (submission_id, phase_name, submission_id, int(truncated)),
                    )
                    if getattr(self.providers, "log_exporter", None) is not None:
                        now = now_iso()
                        connection.execute(
                            "INSERT INTO submission_log_exports(submission_id,phase,status,created_at,updated_at) VALUES (?,?,'pending',?,?) "
                            "ON CONFLICT(submission_id,phase) DO UPDATE SET status=CASE WHEN submission_log_exports.status IN ('exported','exporting') "
                            "THEN submission_log_exports.status ELSE 'pending' END,updated_at=excluded.updated_at",
                            (submission_id, phase_name, now, now),
                        )
        finally:
            connection.close()

    def _fence(self, connection, submission_id: str, worker: str, generation: int) -> None:
        require_job_lease(connection, submission_id, worker, generation)

    def _heartbeat(self, submission_id: str, worker: str, generation: int) -> None:
        heartbeat_connection = connect(self.database_path)
        try:
            with transaction(heartbeat_connection):
                if not heartbeat_job(heartbeat_connection, submission_id, worker, generation, self.lease_seconds):
                    raise LeaseLost("build lease was adopted by another worker")
        finally:
            heartbeat_connection.close()

    def _execute_local_with_heartbeat(self, request: BuildRequest, submission_id: str, worker: str, generation: int):
        cancellation = threading.Event()
        request = replace(request, cancellation_event=cancellation)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="toolbox-build")
        future = executor.submit(self.providers.build.execute, request)
        try:
            while True:
                try:
                    return future.result(timeout=self.heartbeat_interval)
                except FutureTimeout:
                    self._heartbeat(submission_id, worker, generation)
        except LeaseLost:
            cancellation.set()
            try:
                future.result(timeout=5)
            except Exception:
                pass
            raise
        finally:
            cancellation.set()
            executor.shutdown(wait=True, cancel_futures=True)

    def _execute_remote_with_heartbeat(self, request: BuildRequest, submission_id: str, worker: str, generation: int):
        persisted_connection = connect(self.database_path)
        try:
            persisted = persisted_connection.execute(
                "SELECT start_marker,external_id,input_version_id,input_archive_sha256 FROM build_jobs WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
        finally:
            persisted_connection.close()
        marker = str(persisted["start_marker"]) if persisted and persisted["start_marker"] else (request.marker or f"{submission_id}:{generation}:{request.input_sha256}")
        request = BuildRequest(
            request.project_slug, request.snapshot_root, request.output_root, request.timeout_seconds,
            request.input_sha256, marker, request.cancellation_event,
        )
        anchors = {
            "input_version_id": str(persisted["input_version_id"] or "") if persisted else "",
            "input_archive_sha256": str(persisted["input_archive_sha256"] or "") if persisted else "",
        }
        if bool(anchors["input_version_id"]) != bool(anchors["input_archive_sha256"]):
            raise BuildFailed("remote_input_anchor_missing")
        handle = BuildHandle(str(persisted["external_id"]), marker) if persisted and persisted["external_id"] else None
        if handle is not None and not all(anchors.values()):
            raise BuildFailed("remote_input_anchor_missing")
        if handle is None:
            marker_connection = connect(self.database_path)
            try:
                with transaction(marker_connection):
                    self._fence(marker_connection, submission_id, worker, generation)
                    marker_connection.execute(
                        "UPDATE build_jobs SET start_marker=COALESCE(start_marker,?) WHERE submission_id=?",
                        (marker, submission_id),
                    )
            finally:
                marker_connection.close()
            matches = self.providers.build.reconcile(marker, self.build_timeout_seconds + self.lease_seconds)
            if len(matches) > 1:
                raise BuildFailed("remote_start_unknown")
            if matches:
                if not all(anchors.values()):
                    raise BuildFailed("remote_input_anchor_missing")
                handle = matches[0]
            else:
                if not all(anchors.values()):
                    try:
                        prepared = self.providers.build.prepare_input(request)
                        anchors = {
                            "input_version_id": str(prepared.get("input_version_id") or ""),
                            "input_archive_sha256": str(prepared.get("input_archive_sha256") or ""),
                        }
                    except Exception as exc:
                        raise BuildFailed("remote_input_prepare_failed") from exc
                    if not all(anchors.values()):
                        raise BuildFailed("remote_input_anchor_missing")
                    anchor_connection = connect(self.database_path)
                    try:
                        with transaction(anchor_connection):
                            self._fence(anchor_connection, submission_id, worker, generation)
                            anchor_connection.execute(
                                "UPDATE build_jobs SET input_version_id=?,input_archive_sha256=? WHERE submission_id=?",
                                (anchors["input_version_id"], anchors["input_archive_sha256"], submission_id),
                            )
                    finally:
                        anchor_connection.close()
                try:
                    handle = self.providers.build.start(request, anchors)
                except Exception as exc:
                    matches = self.providers.build.reconcile(marker, self.build_timeout_seconds + self.lease_seconds)
                    if len(matches) != 1:
                        raise BuildFailed("remote_start_unknown") from exc
                    handle = matches[0]
        handle_connection = connect(self.database_path)
        try:
            with transaction(handle_connection):
                self._fence(handle_connection, submission_id, worker, generation)
                handle_connection.execute("UPDATE build_jobs SET external_id=? WHERE submission_id=?", (handle.id, submission_id))
                handle_connection.execute("UPDATE submission_builds SET external_id=? WHERE submission_id=?", (handle.id, submission_id))
        finally:
            handle_connection.close()
        deadline = time.monotonic() + request.timeout_seconds
        while time.monotonic() < deadline:
            poll = self.providers.build.poll(handle)
            if poll.status == "succeeded":
                return self.providers.build.fetch_outputs(handle, {
                    "input_sha256": request.input_sha256 or "",
                    "harness_sha256": getattr(self.providers.build, "harness_sha256", ""),
                    **anchors,
                })
            if poll.status in {"failed", "unknown"}:
                raise BuildFailed(poll.error_code or "codebuild_failed")
            self._heartbeat(submission_id, worker, generation)
            time.sleep(min(self.heartbeat_interval, 1.0))
        raise BuildFailed("build_timeout")

    def _execute_build(self, request: BuildRequest, submission_id: str, worker: str, generation: int):
        if getattr(self.providers.build, "provider", "") == "codebuild":
            return self._execute_remote_with_heartbeat(request, submission_id, worker, generation)
        return self._execute_local_with_heartbeat(request, submission_id, worker, generation)

    def _reserve_capacity(self, connection, submission_id: str, slug: str) -> None:
        building_global = int(connection.execute("SELECT COUNT(*) FROM submission_records WHERE state='building'").fetchone()[0])
        building_project = int(connection.execute("SELECT COUNT(*) FROM submission_records WHERE state='building' AND project_slug=?", (slug,)).fetchone()[0])
        if building_global >= self.max_builds_global or building_project >= self.max_builds_per_project:
            raise PublicError("build_capacity_exceeded", "build capacity is exhausted", 429, recoverable=True)
        reserve_bytes = self.snapshot_max_bytes + self.artifact_max_bytes
        held = int(connection.execute("SELECT COALESCE(SUM(bytes),0) FROM storage_reservations WHERE state='held'").fetchone()[0])
        if held + reserve_bytes > self.storage_spool_max_bytes:
            raise PublicError("storage_capacity_exceeded", "storage spool capacity is exhausted", 507, recoverable=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.runtime_root).free < self.storage_min_free_bytes + reserve_bytes:
            raise PublicError("storage_space_insufficient", "insufficient free storage for submission", 507, recoverable=True)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self.build_timeout_seconds + self.lease_seconds)).isoformat()
        connection.execute(
            "INSERT INTO storage_reservations(id,kind,owner_id,bytes,state,expires_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            ("res_" + secrets.token_hex(16), "submission", submission_id, reserve_bytes, "held", expires, now_iso()),
        )

    @staticmethod
    def _finish_capacity(connection, submission_id: str, state: str) -> None:
        connection.execute(
            "UPDATE storage_reservations SET state=?,updated_at=? WHERE owner_id=? AND kind='submission' AND state='held'",
            (state, now_iso(), submission_id),
        )

    def reserve(self, *, slug: str, actor: str, actor_roles: set[str] | tuple[str, ...] = (), author_display: str | None = None, title: str, description: str, idempotency_key: str, predecessor_id: str | None = None, correlation_id: str | None = None, route_template: str = "/api/submissions") -> tuple[dict[str, object], bool]:
        if not registry.project_exists(slug):
            raise PublicError("not_found", "project not found", 404)
        payload = {"project_slug": slug, "title": title, "description": description, "predecessor_id": predecessor_id}
        request_sha = canonical_request_hash(payload)
        operation_id = "op_" + secrets.token_hex(16)
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                idem = reserve(connection, scope="submission", actor_id=actor, route_template=route_template, resource_id=slug, key=idempotency_key, request_sha256=request_sha, operation_id=operation_id)
                if idem.kind != "new":
                    row = connection.execute(
                        "SELECT s.* FROM submission_records s JOIN idempotency_records i ON i.subject_id=s.id WHERE i.operation_id=?",
                        (idem.operation_id,),
                    ).fetchone()
                    if row is not None:
                        response = build_operation_response(connection, idem.operation_id, row)
                        if idem.response is not None:
                            complete(connection, idem.operation_id, int(idem.status_code or operation_success_status(connection, idem.operation_id)), response)
                        return response, False
                    return {
                        "operation_id": idem.operation_id,
                        "submission_id": None,
                        "status": "building",
                        "poll_url": f"/api/submission-operations/{idem.operation_id}",
                    }, False
                repo = PipelineRepository(connection)
                if predecessor_id:
                    predecessor = repo.get(predecessor_id)
                    terminal = {"build_failed", "changes_requested", "rejected", "approved", "published"}
                    if predecessor is None or predecessor["project_slug"] != slug or predecessor["state"] not in terminal:
                        raise PublicError("invalid_predecessor", "predecessor must be a terminal submission from the same project", 422)
                version = repo.next_version(slug)
                submission_id = "sub_" + secrets.token_hex(16)
                self._reserve_capacity(connection, submission_id, slug)
                repo.insert_building(
                    submission_id=submission_id, slug=slug, version=version, actor=actor,
                    author_display=author_display, title=title, description=description, predecessor_id=predecessor_id,
                    correlation_id=correlation_id, build_provider=getattr(self.providers.build, "provider", self.providers.build.__class__.__name__),
                    actor_roles=tuple(sorted(actor_roles)),
                )
                for phase in _PHASES:
                    self._set_phase(connection, submission_id, phase, "succeeded" if phase == "validation" else "pending")
                connection.execute("UPDATE idempotency_records SET subject_id=? WHERE operation_id=?", (submission_id, operation_id))
                return self._response(operation_id, repo.get(submission_id), connection), True
        finally:
            connection.close()

    def operation_status_code(self, operation_id: str, default: int = 202) -> int:
        connection = connect(self.database_path)
        try:
            row = connection.execute("SELECT status_code FROM idempotency_records WHERE operation_id=?", (operation_id,)).fetchone()
            return int(row["status_code"]) if row and row["status_code"] is not None else default
        finally:
            connection.close()

    def run(self, operation_id: str, submission_id: str) -> dict[str, object]:
        worker = "worker-" + uuid.uuid4().hex
        connection = connect(self.database_path)
        current_phase = "validation"
        generation = None
        try:
            with transaction(connection):
                generation = claim_job(connection, submission_id, worker, self.lease_seconds)
            if generation is None:
                row = PipelineRepository(connection).get(submission_id)
                return self._response(operation_id, row, connection)
            row = PipelineRepository(connection).get(submission_id)
            if row is None:
                raise RuntimeError("reserved submission is missing")
            slug = row["project_slug"]
            recovering_snapshot = bool(row["snapshot_ref"])
            if recovering_snapshot:
                if not row["snapshot_manifest_json"] or not row["input_sha256"] or not row["git_json"]:
                    raise PublicError(
                        "sealed_snapshot_unavailable",
                        "the original sealed snapshot cannot be recovered",
                        409,
                        recoverable=True,
                    )
                try:
                    git_data = json.loads(row["git_json"])
                except (TypeError, ValueError) as exc:
                    raise PublicError(
                        "sealed_snapshot_invalid",
                        "the original sealed snapshot metadata is invalid",
                        409,
                        recoverable=True,
                    ) from exc
            else:
                git_data = self.providers.git.inspect(self.frontend_root.parent).__dict__
                if self.git_require_clean and git_data.get("dirty") is not False:
                    raise PublicError("git_not_clean", "submission requires a clean Git checkout", 409)
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                self._set_phase(connection, submission_id, "validation", "running")
            self._heartbeat(submission_id, worker, generation)
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                self._set_phase(connection, submission_id, "validation", "succeeded")
                self._set_phase(connection, submission_id, "snapshot", "running")
            current_phase = "snapshot"
            if recovering_snapshot:
                try:
                    snapshot = load_sealed_snapshot(
                        resolve_runtime_ref(self.runtime_root, str(row["snapshot_ref"])),
                        str(slug),
                        str(row["snapshot_manifest_json"]),
                        str(row["input_sha256"]),
                        max_files=self.snapshot_max_files,
                        max_bytes=self.snapshot_max_bytes,
                    )
                except (OSError, ValueError) as exc:
                    raise PublicError(
                        "sealed_snapshot_invalid",
                        "the original sealed snapshot failed integrity verification",
                        409,
                        recoverable=True,
                    ) from exc
            else:
                snapshot_destination = self.runtime_root / "inputs" / f"{submission_id}-g{generation}"
                with workspace_lock(self.database_path, str(slug)):
                    require_workspace_ready(self.database_path, str(slug))
                    snapshot = SafeSnapshotBuilder(
                        registry.FRONTEND_PROJECTS_DIR,
                        registry.BACKEND_PROJECTS_DIR,
                        max_files=self.snapshot_max_files,
                        max_bytes=self.snapshot_max_bytes,
                        extra_exclusions=self.file_exclusions,
                    ).build(slug, snapshot_destination)
                with transaction(connection):
                    self._fence(connection, submission_id, worker, generation)
                    connection.execute(
                        "UPDATE submission_records SET snapshot_ref=?,snapshot_manifest_json=?,input_sha256=?,git_json=?,updated_at=? WHERE id=? AND state='building'",
                        (
                            encode_runtime_ref(self.runtime_root, snapshot.root),
                            snapshot_manifest_json(snapshot),
                            snapshot.input_sha256,
                            json.dumps(git_data, sort_keys=True),
                            now_iso(),
                            submission_id,
                        ),
                    )
            self._heartbeat(submission_id, worker, generation)
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                self._set_phase(connection, submission_id, "snapshot", "succeeded")
                self._set_phase(connection, submission_id, "build", "running")
            current_phase = "build"
            build_output = self.runtime_root / "builds" / f"{submission_id}-g{generation}"
            marker = f"{submission_id}:{generation}:{snapshot.input_sha256}"
            outputs = self._execute_build(BuildRequest(slug, snapshot.root, build_output, timeout_seconds=self.build_timeout_seconds, input_sha256=snapshot.input_sha256, marker=marker), submission_id, worker, generation)
            if not outputs.ok:
                raise BuildFailed(outputs.error_code or "build_failed", phases=outputs.phases)
            self._persist_logs(submission_id, outputs.phases)
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                connection.execute(
                    "UPDATE submission_records SET build_output_ref=?,updated_at=? WHERE id=? AND state='building'",
                    (encode_runtime_ref(self.runtime_root, build_output), now_iso(), submission_id),
                )
                self._set_phase(connection, submission_id, "build", "succeeded")
            preview_manager = PreviewManager(self.database_path, self.runtime_root / "previews", preview_origin=self.preview_origin)
            try:
                if not (build_output / "preview").is_dir():
                    raise PublicError("preview_unavailable", "build provider did not return a sealed preview", 409)
                preview_manager.seal(
                    subject_kind="submission", subject_id=submission_id, source=build_output / "preview",
                    input_sha256=snapshot.input_sha256, harness_sha256=outputs.harness_sha256,
                    runtime_root=snapshot.root / "backend", project_slug=slug,
                )
            except PublicError as exc:
                preview_manager.unavailable(subject_kind="submission", subject_id=submission_id, input_sha256=snapshot.input_sha256, harness_sha256=outputs.harness_sha256, code=exc.code)
            current_phase = "package"
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                self._set_phase(connection, submission_id, "package", "running")
            package_path = self.runtime_root / "staging" / f"{submission_id}-g{generation}.zip"
            package = create_reproducible_package(
                snapshot,
                package_path,
                submission_id=submission_id,
                version=int(row["version_seq"]),
                harness_sha256=outputs.harness_sha256,
                git=git_data,
                max_bytes=self.artifact_max_bytes,
                timeout_seconds=self.build_timeout_seconds,
            )
            if package.size > self.artifact_max_bytes:
                raise PublicError("artifact_too_large", "artifact exceeds configured size limit", 413)
            object_id = f"art:v1:{submission_id}:{snapshot.input_sha256}:{outputs.harness_sha256}"
            artifact_provider = getattr(self.providers.artifacts, "provider", "unknown")
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                self._set_phase(connection, submission_id, "package", "succeeded")
                self._set_phase(connection, submission_id, "register", "running")
                now = now_iso()
                connection.execute(
                    "INSERT OR IGNORE INTO artifact_objects(object_id,expected_sha256,expected_size,state,provider,submission_id,manifest_json,input_sha256,harness_sha256,created_at,updated_at) VALUES (?,?,?,'reserved',?,?,?,?,?,?,?)",
                    (object_id, package.sha256, package.size, artifact_provider, submission_id, json.dumps(package.manifest, sort_keys=True, separators=(",", ":")), snapshot.input_sha256, outputs.harness_sha256, now, now),
                )
                PipelineRepository(connection).mark_built(submission_id, snapshot.input_sha256, outputs.harness_sha256)
                connection.execute("UPDATE submission_records SET git_json=? WHERE id=?", (json.dumps(git_data, sort_keys=True), submission_id))
            self._heartbeat(submission_id, worker, generation)
            ref = self.providers.artifacts.put_if_absent(object_id, package.path, package.size, package.sha256, {"submission_id": submission_id})
            self._heartbeat(submission_id, worker, generation)
            object_stat = self.providers.artifacts.stat(ref)
            if (object_stat.size, object_stat.sha256) != (package.size, package.sha256):
                with transaction(connection):
                    self._fence(connection, submission_id, worker, generation)
                    connection.execute("UPDATE artifact_objects SET state='uncertain',updated_at=? WHERE object_id=?", (now_iso(), object_id))
                raise BuildFailed("artifact_integrity_failed")
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                connection.execute("UPDATE artifact_objects SET state='stored',ref=?,updated_at=? WHERE object_id=? AND state IN ('reserved','uncertain')", (ref.key, now_iso(), object_id))
            current_phase = "register"
            with transaction(connection):
                self._fence(connection, submission_id, worker, generation)
                repo = PipelineRepository(connection)
                repo.commit_artifact(submission_id, object_id=object_id, ref=ref.key, sha256=package.sha256, size=package.size, manifest=package.manifest, artifact_provider=ref.provider)
                self._set_phase(connection, submission_id, "register", "succeeded")
                self._set_phase(connection, submission_id, "notify", "running")
                submitted = repo.get(submission_id)
                effect_ids = enqueue_notification(
                    connection,
                    submitted,
                    state="submitted",
                    review_base_url=self.review_base_url,
                    channels=notification_channels(self.providers),
                )
                finish_job(connection, submission_id, worker, generation, success=True)
                self._finish_capacity(connection, submission_id, "committed")
            for effect_id in effect_ids:
                try:
                    OutboxDispatcher(connection, self.providers).dispatch(effect_id)
                except Exception:
                    with transaction(connection):
                        reconcile_notification_phase(connection, submission_id)
            with transaction(connection):
                notify_status = reconcile_notification_phase(connection, submission_id)
                response = self._response(operation_id, PipelineRepository(connection).get(submission_id), connection)
                if notify_status in {"succeeded", "failed", "delivery_unknown"}:
                    complete(connection, operation_id, operation_success_status(connection, operation_id), response)
            return response
        except LeaseLost:
            return self._response(operation_id, PipelineRepository(connection).get(submission_id), connection)
        except Exception as exc:
            code = exc.code if isinstance(exc, (BuildFailed, PublicError)) else "build_internal_error"
            if isinstance(exc, BuildFailed):
                try:
                    self._persist_logs(submission_id, exc.phases)
                except Exception:
                    pass
            with transaction(connection):
                if generation is not None:
                    self._fence(connection, submission_id, worker, generation)
                self._set_phase(connection, submission_id, current_phase, "failed", code)
                repo = PipelineRepository(connection)
                row = repo.get(submission_id)
                if row and row["state"] == "building":
                    repo.mark_failed(submission_id, code)
                if generation is not None:
                    finish_job(connection, submission_id, worker, generation, success=False, error_code=code)
                self._finish_capacity(connection, submission_id, "released")
                response = self._response(operation_id, repo.get(submission_id), connection)
                status_code = exc.status_code if isinstance(exc, PublicError) else (504 if code == "build_timeout" else 422 if isinstance(exc, BuildFailed) else 500)
                complete(connection, operation_id, status_code, response)
            return response
        finally:
            connection.close()

    def _response(self, operation_id: str, row, connection=None) -> dict[str, object]:
        own_connection = connection is None
        connection = connection or connect(self.database_path)
        try:
            return build_operation_response(connection, operation_id, row)
        finally:
            if own_connection:
                connection.close()
