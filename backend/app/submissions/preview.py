from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import stat
import threading
import unicodedata
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from app.core.errors import PublicError
from app.projects.runtime import SealedRuntimeSource
from app.projects.safe_fs import walk_regular_files_nofollow
from app.submissions.database import connect, transaction
from app.submissions.repository_v2 import now_iso
from app.submissions.runtime_refs import encode_runtime_ref, resolve_runtime_ref
from app.submissions.snapshot import _open_regular_nofollow


_READ_ONLY_MODE = stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH
_PROMOTION_LOCKS_GUARD = threading.Lock()
_PROMOTION_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _promotion_lock(path: Path) -> Iterator[None]:
    key = str(path.absolute())
    with _PROMOTION_LOCKS_GUARD:
        process_lock = _PROMOTION_LOCKS.setdefault(key, threading.Lock())
    with process_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _make_read_only(path: Path, handle: BinaryIO) -> None:
    if os.name == "nt":
        os.chmod(path, _READ_ONLY_MODE, follow_symlinks=False)
    else:
        os.fchmod(handle.fileno(), _READ_ONLY_MODE)


def _hash_handle(handle: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    while chunk := handle.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _runtime_archive(opened: list[tuple[BinaryIO, dict[str, object]]]) -> bytes:
    payload = io.BytesIO()
    paths = {str(item["path"]) for _handle, item in opened}
    package_markers = ("app/__init__.py", "app/projects/__init__.py")
    project_paths = sorted(path for path in paths if path.startswith("app/projects/") and path.count("/") >= 3)
    if project_paths:
        package_markers += ("/".join(project_paths[0].split("/")[:3]) + "/__init__.py",)
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for marker in package_markers:
            if marker not in paths:
                info = zipfile.ZipInfo(marker, (1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100444 << 16
                archive.writestr(info, b"")
        for handle, item in sorted(opened, key=lambda pair: str(pair[1]["path"])):
            handle.seek(0)
            content = handle.read()
            if (len(content), hashlib.sha256(content).hexdigest()) != (item["size"], item["sha256"]):
                raise ValueError("preview runtime changed while preparing execution")
            info = zipfile.ZipInfo(str(item["path"]), (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100444 << 16
            archive.writestr(info, content)
    return payload.getvalue()


@contextmanager
def _sealed_runtime_source(opened: list[tuple[BinaryIO, dict[str, object]]]) -> Iterator[SealedRuntimeSource]:
    if os.name == "nt":
        raise OSError("sealed runtime archive is only used on POSIX")
    if not hasattr(os, "memfd_create") or not Path("/proc/self/fd").is_dir():
        raise OSError("descriptor-bound runtime execution is unavailable")
    flags = int(getattr(os, "MFD_CLOEXEC", 0)) | int(getattr(os, "MFD_ALLOW_SEALING", 0))
    descriptor = os.memfd_create("toolbox-preview-runtime", flags)
    try:
        archive = _runtime_archive(opened)
        view = memoryview(archive)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            import fcntl

            seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        except (AttributeError, OSError):
            pass
        yield SealedRuntimeSource(Path(f"/proc/self/fd/{descriptor}"), (descriptor,))
    finally:
        os.close(descriptor)


def _safe_relative(value: str) -> PurePosixPath:
    if not value or len(value) > 500 or "\\" in value or "\x00" in value:
        raise PublicError("invalid_preview_path", "invalid preview path", 404)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PublicError("invalid_preview_path", "invalid preview path", 404)
    return path


def _regular_files(root: Path, *, code: str, message: str) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    try:
        for relative, source in walk_regular_files_nofollow(root):
            with source.open() as (handle, _info):
                size, digest = _hash_handle(handle)
            files.append({"path": relative, "size": size, "sha256": digest})
    except OSError as exc:
        raise PublicError(code, message, 409) from exc
    return files


def _verify_regular_files(
    root: Path,
    expected: list[dict[str, object]],
    *,
    code: str,
    message: str,
    make_read_only: bool = False,
    include_file_id: bool = False,
) -> list[dict[str, object]]:
    expected_by_path = {str(item["path"]): item for item in expected}
    verified: list[dict[str, object]] = []
    try:
        for relative, source in walk_regular_files_nofollow(root):
            item = expected_by_path.get(relative)
            if item is None:
                raise OSError("preview tree gained an unexpected file")
            with source.open() as (handle, info):
                size, digest = _hash_handle(handle)
                if (size, digest) != (item["size"], item["sha256"]):
                    raise OSError("preview file content changed")
                if make_read_only:
                    _make_read_only(source.path, handle)
                result = dict(item)
                if include_file_id:
                    result["file_id"] = f"{info.st_dev}:{info.st_ino}"
                verified.append(result)
        if {str(item["path"]) for item in verified} != set(expected_by_path):
            raise OSError("preview tree lost an expected file")
    except OSError as exc:
        raise PublicError(code, message, 409) from exc
    return verified


def preview_tree_manifest_json(root: Path, *, max_files: int, max_bytes: int) -> str:
    files = sorted(
        _regular_files(
            Path(root),
            code="preview_incompatible",
            message="preview output contains a link, reparse point, or non-regular entry",
        ),
        key=lambda item: str(item["path"]),
    )
    if len(files) > max_files or sum(int(item["size"]) for item in files) > max_bytes:
        raise PublicError("preview_limit", "preview output exceeds configured limits", 413)
    canonical_files = json.dumps(files, sort_keys=True, separators=(",", ":"))
    manifest = {
        "version": 1,
        "tree_sha256": hashlib.sha256(canonical_files.encode("utf-8")).hexdigest(),
        "files": files,
    }
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def load_sealed_preview_tree(root: Path, manifest_json: str, *, max_files: int, max_bytes: int) -> list[dict[str, object]]:
    try:
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("sealed preview output manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"version", "tree_sha256", "files"}:
        raise ValueError("sealed preview output manifest is invalid")
    if manifest["version"] != 1 or not isinstance(manifest["files"], list):
        raise ValueError("sealed preview output manifest is invalid")
    files: list[dict[str, object]] = []
    paths: set[str] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ValueError("sealed preview output manifest file is invalid")
        path = item["path"]
        size = item["size"]
        sha256 = item["sha256"]
        if not isinstance(path, str):
            raise ValueError("sealed preview output manifest file is invalid")
        try:
            relative = _safe_relative(path)
        except PublicError as exc:
            raise ValueError("sealed preview output manifest path is invalid") from exc
        if (
            relative.as_posix() != path
            or unicodedata.normalize("NFC", path) != path
            or path in paths
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError("sealed preview output manifest file is invalid")
        paths.add(path)
        files.append({"path": path, "size": size, "sha256": sha256})
    files.sort(key=lambda item: str(item["path"]))
    if len(files) > max_files or sum(int(item["size"]) for item in files) > max_bytes:
        raise ValueError("sealed preview output exceeds configured limits")
    canonical_files = json.dumps(files, sort_keys=True, separators=(",", ":"))
    if manifest["tree_sha256"] != hashlib.sha256(canonical_files.encode("utf-8")).hexdigest():
        raise ValueError("sealed preview output manifest digest is invalid")
    return _verify_regular_files(
        Path(root),
        files,
        code="preview_integrity_failed",
        message="sealed preview output changed or contains a link",
    )


def _copy_regular_files(source_root: Path, destination: Path, expected: list[dict[str, object]]) -> None:
    expected_by_path = {str(item["path"]): item for item in expected}
    copied: set[str] = set()
    try:
        for relative, source in walk_regular_files_nofollow(source_root):
            item = expected_by_path.get(relative)
            if item is None:
                raise OSError("preview source gained an unexpected file")
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with source.open() as (input_file, _info), target.open("xb") as output:
                while chunk := input_file.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if (size, digest.hexdigest()) != (item["size"], item["sha256"]):
                raise OSError("preview source changed while copying")
            copied.add(relative)
        if copied != set(expected_by_path):
            raise OSError("preview source lost an expected file")
    except OSError as exc:
        raise PublicError("preview_incompatible", "preview source changed or contains a link", 409) from exc


class PreviewManager:
    def __init__(self, database_path: Path, root: Path, *, preview_origin: str = "http://127.0.0.1:5001") -> None:
        self.database_path = Path(database_path)
        self.root = Path(root)
        self.preview_origin = preview_origin.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def seal(self, *, subject_kind: str, subject_id: str, source: Path, input_sha256: str, harness_sha256: str, runtime_root: Path | None = None, project_slug: str | None = None) -> dict[str, object]:
        source = Path(source).absolute()
        files = _regular_files(
            source,
            code="preview_incompatible",
            message="preview contains a link, reparse point, or non-regular entry",
        )
        combined = hashlib.sha256()
        for item in files:
            combined.update(str(item["path"]).encode("utf-8") + b"\0" + bytes.fromhex(str(item["sha256"])))
        if not files or not any(item["path"] == "index.html" for item in files):
            raise PublicError("preview_incompatible", "sealed preview has no index", 409)
        runtime_files: list[dict[str, object]] = []
        runtime_ref = None
        if runtime_root is not None:
            runtime = Path(runtime_root).absolute()
            slug = project_slug or (subject_id if subject_kind == "workspace" else "")
            runtime_files = _regular_files(
                runtime,
                code="preview_runtime_unavailable",
                message="sealed preview runtime contains a link, reparse point, or non-regular entry",
            )
            required_route = f"app/projects/{slug}/routes.py" if slug else ""
            if not runtime_files or required_route not in {item["path"] for item in runtime_files}:
                raise PublicError("preview_runtime_unavailable", "sealed preview runtime is unavailable", 409)
            runtime_files = _verify_regular_files(
                runtime,
                runtime_files,
                code="preview_runtime_unavailable",
                message="sealed preview runtime changed or contains a link",
                make_read_only=True,
            )
            runtime_ref = encode_runtime_ref(self.root.parent, runtime)
            for item in runtime_files:
                combined.update(b"runtime\0" + str(item["path"]).encode("utf-8") + b"\0" + bytes.fromhex(str(item["sha256"])))
        digest = hashlib.sha256((subject_kind + "\0" + subject_id + "\0" + input_sha256 + "\0" + harness_sha256).encode("utf-8") + combined.digest()).hexdigest()
        object_id = "prv_" + digest[:32]
        destination = self.root / digest[:2] / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging: Path | None = None
        if not destination.exists():
            staging = destination.with_name(f".{destination.name}.staging-{os.getpid()}-{secrets.token_hex(8)}")
            staging.mkdir(parents=False, exist_ok=False)
            _copy_regular_files(source, staging, files)
        collided_staging: Path | None = None
        lock_path = destination.with_name(f".{destination.name}.promotion.lock")
        with _promotion_lock(lock_path):
            if staging is not None:
                if destination.exists():
                    collided_staging = staging
                else:
                    os.rename(staging, destination)
            sealed_files = _verify_regular_files(
                destination,
                files,
                code="preview_integrity_failed",
                message="preview object collision",
                make_read_only=True,
                include_file_id=True,
            )
        manifest = {"version": 2, "digest": digest, "files": sealed_files, "runtime_files": runtime_files}
        if collided_staging is not None:
            reservation_id = "prq_" + secrets.token_hex(16)
            quarantine = destination.with_name(f".{destination.name}.quarantine-{reservation_id}")
            connection = connect(self.database_path)
            try:
                with transaction(connection):
                    connection.execute(
                        "INSERT INTO storage_reservations(id,kind,owner_id,bytes,state,updated_at) VALUES (?,?,?,?,?,?)",
                        (reservation_id, "preview-race", collided_staging.name, sum(int(item["size"]) for item in files), "held", now_iso()),
                    )
            finally:
                connection.close()
            os.rename(collided_staging, quarantine)
            connection = connect(self.database_path)
            try:
                with transaction(connection):
                    connection.execute(
                        "UPDATE storage_reservations SET owner_id=?,state='quarantined',updated_at=? WHERE id=? AND state='held'",
                        (quarantine.name, now_iso(), reservation_id),
                    )
            finally:
                connection.close()
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                now = now_iso()
                connection.execute(
                    "INSERT OR IGNORE INTO preview_objects(id,subject_kind,subject_id,input_sha256,harness_sha256,digest,state,manifest_json,root_ref,runtime_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,'ready',?,?,?,?,?)",
                    (object_id, subject_kind, subject_id, input_sha256, harness_sha256, digest, json.dumps(manifest, sort_keys=True, separators=(",", ":")), digest, runtime_ref, now, now),
                )
                connection.execute(
                    "UPDATE preview_objects SET state='ready',error_code=NULL,manifest_json=?,root_ref=?,runtime_ref=?,updated_at=? WHERE id=?",
                    (json.dumps(manifest, sort_keys=True, separators=(",", ":")), digest, runtime_ref, now, object_id),
                )
                if subject_kind == "workspace":
                    superseded = connection.execute(
                        "SELECT id FROM preview_objects WHERE subject_kind='workspace' AND subject_id=? AND id<>? AND state='ready'",
                        (subject_id, object_id),
                    ).fetchall()
                    superseded_ids = [str(item["id"]) for item in superseded]
                    if superseded_ids:
                        placeholders = ",".join("?" for _item in superseded_ids)
                        connection.execute(
                            f"UPDATE preview_objects SET state='unavailable',error_code='preview_superseded',updated_at=? WHERE id IN ({placeholders})",
                            (now, *superseded_ids),
                        )
                        connection.execute(
                            f"UPDATE preview_grants SET consumed_at=COALESCE(consumed_at,?) WHERE preview_id IN ({placeholders}) AND consumed_at IS NULL",
                            (now, *superseded_ids),
                        )
                        connection.execute(
                            f"UPDATE preview_sessions SET expires_at=? WHERE preview_id IN ({placeholders}) AND expires_at>?",
                            (now, *superseded_ids, now),
                        )
                row = connection.execute("SELECT * FROM preview_objects WHERE subject_kind=? AND subject_id=? AND digest=?", (subject_kind, subject_id, digest)).fetchone()
                return dict(row)
        finally:
            connection.close()

    def unavailable(self, *, subject_kind: str, subject_id: str, input_sha256: str, harness_sha256: str, code: str) -> None:
        digest = hashlib.sha256(f"{subject_kind}\0{subject_id}\0{input_sha256}\0{harness_sha256}".encode()).hexdigest()
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                now = now_iso()
                connection.execute("INSERT OR IGNORE INTO preview_objects(id,subject_kind,subject_id,input_sha256,harness_sha256,digest,state,error_code,created_at,updated_at) VALUES (?,?,?,?,?,?,'unavailable',?,?,?)", ("prv_" + digest[:32], subject_kind, subject_id, input_sha256, harness_sha256, digest, code, now, now))
        finally:
            connection.close()

    def create_grant(self, preview_id: str, *, subject_kind: str, subject_id: str, actor: str, ttl_seconds: int = 300) -> dict[str, str]:
        ttl = min(max(ttl_seconds, 1), 300)
        nonce = secrets.token_urlsafe(32)
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        grant_id = "grt_" + secrets.token_hex(16)
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute("SELECT * FROM preview_objects WHERE id=? AND subject_kind=? AND subject_id=?", (preview_id, subject_kind, subject_id)).fetchone()
                if row is None:
                    raise PublicError("not_found", "preview not found", 404)
                if row["state"] != "ready":
                    raise PublicError("preview_building" if row["state"] == "building" else "preview_unavailable", "preview is not ready", 409)
                if not row["runtime_ref"]:
                    raise PublicError("preview_runtime_unavailable", "preview runtime is not sealed", 409)
                now = datetime.now(timezone.utc)
                connection.execute("INSERT INTO preview_grants(id,preview_id,subject_kind,subject_id,nonce_sha256,actor_id,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)", (grant_id, preview_id, subject_kind, subject_id, nonce_hash, actor, (now + timedelta(seconds=ttl)).isoformat(), now.isoformat()))
                return {"preview_id": preview_id, "nonce": nonce, "bootstrap_url": f"{self.preview_origin}/bootstrap"}
        finally:
            connection.close()

    def exchange(self, nonce: str, *, preview_id: str, subject_kind: str, subject_id: str, ttl_seconds: int = 900) -> str:
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                now = datetime.now(timezone.utc)
                grant = connection.execute(
                    "SELECT g.* FROM preview_grants g JOIN preview_objects p ON p.id=g.preview_id "
                    "WHERE g.nonce_sha256=? AND g.preview_id=? AND g.subject_kind=? AND g.subject_id=? "
                    "AND g.consumed_at IS NULL AND g.expires_at>? AND p.state='ready'",
                    (nonce_hash, preview_id, subject_kind, subject_id, now.isoformat()),
                ).fetchone()
                if grant is None:
                    raise PublicError("invalid_preview_grant", "preview grant is invalid or expired", 403)
                cursor = connection.execute("UPDATE preview_grants SET consumed_at=? WHERE id=? AND consumed_at IS NULL", (now.isoformat(), grant["id"]))
                if cursor.rowcount != 1:
                    raise PublicError("invalid_preview_grant", "preview grant is invalid or expired", 403)
                connection.execute("INSERT INTO preview_sessions(id,preview_id,token_sha256,subject_kind,subject_id,actor_id,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)", ("psn_" + secrets.token_hex(16), preview_id, token_hash, subject_kind, subject_id, grant["actor_id"], (now + timedelta(seconds=min(ttl_seconds, 3600))).isoformat(), now.isoformat()))
                return token
        finally:
            connection.close()

    def authorize(self, token: str, preview_id: str):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        connection = connect(self.database_path)
        try:
            now = datetime.now(timezone.utc).isoformat()
            return connection.execute("SELECT p.*,s.actor_id AS session_actor_id FROM preview_sessions s JOIN preview_objects p ON p.id=s.preview_id WHERE s.token_sha256=? AND s.preview_id=? AND s.expires_at>? AND p.state='ready'", (token_hash, preview_id, now)).fetchone()
        finally:
            connection.close()

    def authorize_runtime(self, token: str, project_slug: str):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        connection = connect(self.database_path)
        try:
            now = datetime.now(timezone.utc).isoformat()
            row = connection.execute(
                "SELECT p.*,s.actor_id AS session_actor_id FROM preview_sessions s JOIN preview_objects p ON p.id=s.preview_id LEFT JOIN submission_records r ON p.subject_kind='submission' AND r.id=p.subject_id WHERE s.token_sha256=? AND s.expires_at>? AND p.state='ready' AND ((p.subject_kind='workspace' AND p.subject_id=?) OR (p.subject_kind='submission' AND r.project_slug=?)) ORDER BY s.created_at DESC LIMIT 1",
                (token_hash, now, project_slug, project_slug),
            ).fetchone()
            if row is None or not row["runtime_ref"]:
                return None
            return row
        finally:
            connection.close()

    def _mark_integrity_failed(self, preview_id: str) -> None:
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                connection.execute(
                    "UPDATE preview_objects SET state='unavailable',error_code='preview_integrity_failed',updated_at=? WHERE id=? AND state='ready'",
                    (now_iso(), preview_id),
                )
        finally:
            connection.close()

    @contextmanager
    def open_runtime(self, preview_row) -> Iterator[Path]:
        preview_id = str(preview_row["id"])
        consumer_failed = False
        try:
            root = resolve_runtime_ref(self.root.parent, str(preview_row["runtime_ref"]))
            manifest = json.loads(preview_row["manifest_json"])
            expected = manifest.get("runtime_files", [])
            if not isinstance(expected, list) or not expected:
                raise ValueError("preview runtime manifest is invalid")
            actual = _regular_files(
                root,
                code="preview_integrity_failed",
                message="preview runtime contains a link, reparse point, or non-regular entry",
            )
            expected_by_path = {str(item["path"]): item for item in expected}
            actual_by_path = {str(item["path"]): item for item in actual}
            if actual_by_path != expected_by_path:
                raise ValueError("preview runtime file set changed")
            with ExitStack() as stack:
                opened: list[tuple[BinaryIO, dict[str, object]]] = []
                for item in expected:
                    relative = _safe_relative(str(item["path"]))
                    path = root.joinpath(*relative.parts)
                    handle, _info = stack.enter_context(_open_regular_nofollow(path, deny_write=True))
                    if _hash_handle(handle) != (item["size"], item["sha256"]):
                        raise ValueError("preview runtime integrity mismatch")
                    opened.append((handle, item))
                try:
                    if os.name == "nt":
                        yield root
                    else:
                        with _sealed_runtime_source(opened) as source:
                            yield source
                except BaseException:
                    consumer_failed = True
                    raise
                for handle, item in opened:
                    if _hash_handle(handle) != (item["size"], item["sha256"]):
                        raise ValueError("preview runtime changed while in use")
        except PublicError as exc:
            if consumer_failed:
                raise
            if exc.code == "preview_integrity_failed":
                self._mark_integrity_failed(preview_id)
            raise
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            if consumer_failed:
                raise
            self._mark_integrity_failed(preview_id)
            raise PublicError("preview_integrity_failed", "preview runtime integrity failed", 409) from exc

    @contextmanager
    def open_asset(self, preview_row, asset_path: str) -> Iterator[tuple[BinaryIO, str, int]]:
        preview_id = str(preview_row["id"])
        try:
            relative = _safe_relative(asset_path)
            manifest = json.loads(preview_row["manifest_json"])
            item = next((entry for entry in manifest.get("files", []) if entry["path"] == relative.as_posix()), None)
            if item is None:
                raise PublicError("not_found", "preview asset not found", 404)
            root_ref = str(preview_row["root_ref"])
            root = self.root / root_ref[:2] / root_ref
            path = root.joinpath(*relative.parts)
            current = root
            for part in relative.parts[:-1]:
                current = current / part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ValueError("preview path changed")
            with _open_regular_nofollow(path, deny_write=True) as (handle, info):
                size, digest = _hash_handle(handle)
                file_id = f"{info.st_dev}:{info.st_ino}"
                if (size, digest, file_id) != (item["size"], item["sha256"], item["file_id"]):
                    raise ValueError("preview asset integrity mismatch")
                handle.seek(0)
                suffix = path.suffix.lower()
                media = "text/html" if suffix == ".html" else "text/javascript" if suffix == ".js" else "text/css" if suffix == ".css" else "application/octet-stream"
                yield handle, media, size
                if _hash_handle(handle) != (item["size"], item["sha256"]):
                    raise ValueError("preview asset changed while in use")
        except PublicError as exc:
            if exc.code != "not_found":
                self._mark_integrity_failed(preview_id)
            raise
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            self._mark_integrity_failed(preview_id)
            raise PublicError("preview_integrity_failed", "preview unavailable because its sealed content changed", 409) from exc
