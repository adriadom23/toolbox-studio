from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from app.core.studio_config import DEFAULT_DATABASE_PATH
from app.projects.safe_fs import stable_file_target
from app.projects.validation import InvalidProjectNameError, validate_slug
from app.submissions.database import connect, transaction
from app.submissions.migrations import migrate
from app.submissions.repository_v2 import PipelineRepository
from app.submissions.snapshot import _open_regular_nofollow, descriptor_file_id

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_LEGACY_ROOT = BACKEND_DIR / "submissions"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_RATIO = 100


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _same_path_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) == _identity(right)


def _hash_handle(handle) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


@contextmanager
def _open_legacy_source(path: Path, expected: os.stat_result, root: Path):
    if _is_reparse(expected) or not _confined(path, root):
        raise ValueError("legacy source escapes its configured root")
    relative = path.relative_to(root).as_posix()
    with stable_file_target(root, relative) as target:
        if os.name != "nt" and target.parent_descriptor is not None:
            descriptor = os.open(
                target.name,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=target.parent_descriptor,
            )
            opened_context = None
            handle = os.fdopen(descriptor, "rb")
        else:
            opened_context = _open_regular_nofollow(target.path, deny_write=True)
            handle, _opened = opened_context.__enter__()
        try:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
                raise ValueError("legacy source is not a regular file")
            if os.name == "nt":
                if (opened.st_size, opened.st_mtime_ns) != (expected.st_size, expected.st_mtime_ns):
                    raise ValueError("legacy source changed before inspection")
            elif not _same_path_identity(opened, expected):
                raise ValueError("legacy source changed before inspection")
            identity = _identity(opened)
            file_id = descriptor_file_id(handle)
            yield handle, opened, file_id
            if _identity(os.fstat(handle.fileno())) != identity or descriptor_file_id(handle) != file_id:
                raise ValueError("legacy source changed while inspecting archive")
            if os.name != "nt" and target.parent_descriptor is not None:
                current = os.stat(target.name, dir_fd=target.parent_descriptor, follow_symlinks=False)
                if not _same_path_identity(current, opened):
                    raise ValueError("legacy source path changed while inspecting archive")
        finally:
            if opened_context is None:
                handle.close()
            else:
                opened_context.__exit__(None, None, None)


def _confined(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _iter_regular_zips(root: Path, diagnostics: list[dict[str, str]] | None = None):
    root_real = root.resolve(strict=True)
    diagnostics = diagnostics if diagnostics is not None else []

    def reject(path: Path, code: str) -> None:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        diagnostics.append({"relative_path": relative, "code": code})

    def visit(directory: Path):
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name.casefold()):
                info = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                if entry.is_symlink() or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                    reject(path, "legacy_reparse_rejected")
                    continue
                if not _confined(path, root_real):
                    reject(path, "legacy_root_escape")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    yield from visit(path)
                elif stat.S_ISREG(info.st_mode) and entry.name.lower().endswith(".zip"):
                    yield path, info
    root_info = root.lstat()
    if stat.S_ISDIR(root_info.st_mode) and not root.is_symlink() and not _is_reparse(root_info):
        yield from visit(root)


def _safe_entry(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _inspect_zip(handle) -> tuple[str | None, dict[str, object]]:
    try:
        handle.seek(0)
        with zipfile.ZipFile(handle) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                return "legacy_entry_limit", {}
            names: set[str] = set()
            expanded = 0
            inventory = []
            manifest_entry = None
            for item in entries:
                if item.filename in names or not _safe_entry(item.filename):
                    return "legacy_unsafe_path", {}
                names.add(item.filename)
                mode = (item.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK or item.flag_bits & 0x1:
                    return "legacy_unsupported_entry", {}
                if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    return "legacy_unsupported_compression", {}
                if item.file_size and (item.compress_size == 0 or item.file_size > item.compress_size * MAX_RATIO):
                    return "legacy_compression_ratio", {}
                if item.filename == "MANIFEST.json":
                    if item.file_size > MAX_MANIFEST_BYTES:
                        return "legacy_manifest_limit", {}
                    manifest_entry = item
                expanded += item.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    return "legacy_expanded_limit", {}
                inventory.append({"path": item.filename, "size": item.file_size, "compressed_size": item.compress_size})
            manifest_summary: dict[str, object] = {"present": manifest_entry is not None}
            if manifest_entry is not None:
                with archive.open(manifest_entry, "r") as handle:
                    manifest_bytes = handle.read(MAX_MANIFEST_BYTES + 1)
                if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                    return "legacy_manifest_limit", {}
                try:
                    declared = json.loads(manifest_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return "legacy_invalid_manifest", {}
                if not isinstance(declared, dict):
                    return "legacy_invalid_manifest", {}
                manifest_summary = {
                    "present": True,
                    "valid_object": True,
                    "format": str(declared.get("format", ""))[:200],
                    "declared_file_count": len(declared.get("files", [])) if isinstance(declared.get("files"), list) else None,
                    "hashes_trusted": False,
                }
            return None, {"entry_count": len(entries), "expanded_size": expanded, "entries": inventory, "declared_manifest": manifest_summary}
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError):
        return "legacy_invalid_zip", {}


def _known_sources(database_path: Path) -> set[tuple[str, str]]:
    if not database_path.is_file():
        return set()
    try:
        connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        try:
            return {(str(row[0]), str(row[1])) for row in connection.execute("SELECT relative_path,sha256 FROM legacy_sources")}
        finally:
            connection.close()
    except sqlite3.Error:
        return set()


def _source_plan_item(relative: str, handle, opened: os.stat_result, file_id: str) -> dict[str, object]:
    digest = _hash_handle(handle)
    code = None
    details: dict[str, object] = {}
    parts = PurePosixPath(relative).parts
    project_slug = parts[0] if len(parts) >= 2 else ""
    if opened.st_size > MAX_ARCHIVE_BYTES:
        code = "legacy_archive_limit"
    else:
        try:
            slug = validate_slug(project_slug) if project_slug else ""
        except InvalidProjectNameError:
            slug = ""
        if not slug:
            code = "legacy_invalid_project"
        else:
            code, details = _inspect_zip(handle)
    return {
        "relative_path": relative,
        "project_slug": project_slug,
        "sha256": digest,
        "size": int(opened.st_size),
        "mtime_ns": int(opened.st_mtime_ns),
        "file_id": file_id,
        "code": code,
        "inventory": details,
    }


def build_plan(*, root: Path = DEFAULT_LEGACY_ROOT, database_path: Path = DEFAULT_DATABASE_PATH) -> dict[str, object]:
    root = Path(root).resolve()
    known = _known_sources(Path(database_path))
    items: list[dict[str, object]] = []
    diagnostics: list[dict[str, str]] = []
    for path, info in _iter_regular_zips(root, diagnostics):
        relative = path.relative_to(root).as_posix()
        with _open_legacy_source(path, info, root) as (handle, opened, file_id):
            item = _source_plan_item(relative, handle, opened, file_id)
        digest = str(item["sha256"])
        item["status"] = "invalid" if item["code"] else ("indexed" if (relative, digest) in known else "ready")
        items.append(item)
    items.sort(key=lambda item: (int(item["mtime_ns"]), str(item["relative_path"]), str(item["sha256"])))
    payload = {"version": 1, "root": root.as_posix(), "items": items, "diagnostics": diagnostics}
    payload["plan_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return payload


def apply_plan(plan_hash: str, *, root: Path = DEFAULT_LEGACY_ROOT, database_path: Path = DEFAULT_DATABASE_PATH) -> dict[str, object]:
    root = Path(root).resolve()
    current = build_plan(root=root, database_path=database_path)
    if not secrets.compare_digest(str(current["plan_hash"]), plan_hash):
        raise ValueError("legacy plan changed")
    migrate(database_path)
    connection = connect(database_path)
    imported = 0
    observed = 0
    comparison_fields = (
        "relative_path", "project_slug", "sha256", "size", "mtime_ns", "file_id", "code", "inventory",
    )
    try:
        with ExitStack() as sources:
            retained_sources = []
            try:
                for item in current["items"]:
                    relative = str(item["relative_path"])
                    source_path = root.joinpath(*PurePosixPath(relative).parts)
                    expected = source_path.lstat()
                    handle, opened, file_id = sources.enter_context(_open_legacy_source(source_path, expected, root))
                    verified = _source_plan_item(relative, handle, opened, file_id)
                    if any(verified[field] != item[field] for field in comparison_fields):
                        raise ValueError("legacy source changed before apply")
                    retained_sources.append((source_path, opened, handle, file_id, item))
            except (OSError, ValueError) as exc:
                raise ValueError("legacy source changed before apply") from exc

            with transaction(connection):
                repo = PipelineRepository(connection)
                for item in current["items"]:
                    relative, digest = str(item["relative_path"]), str(item["sha256"])
                    if item["status"] == "indexed":
                        continue
                    if item["status"] == "invalid":
                        connection.execute(
                            "INSERT OR IGNORE INTO legacy_observations(relative_path,sha256,code,detail,created_at) VALUES (?,?,?,?,?)",
                            (relative, digest, item["code"], "legacy archive was not indexed", datetime.now(timezone.utc).isoformat()),
                        )
                        observed += 1
                        continue
                    project = connection.execute("SELECT slug FROM projects WHERE slug=?", (item["project_slug"],)).fetchone()
                    if project is None:
                        connection.execute(
                            "INSERT OR IGNORE INTO legacy_observations(relative_path,sha256,code,detail,created_at) VALUES (?,?,?,?,?)",
                            (relative, digest, "legacy_project_missing", "project must be discovered before import", datetime.now(timezone.utc).isoformat()),
                        )
                        observed += 1
                        continue
                    if connection.execute("SELECT 1 FROM legacy_sources WHERE relative_path=? AND sha256=?", (relative, digest)).fetchone():
                        continue
                    submission_id = "sub_" + secrets.token_hex(16)
                    version = repo.next_version(str(item["project_slug"]))
                    timestamp = datetime.fromtimestamp(int(item["mtime_ns"]) / 1_000_000_000, timezone.utc).isoformat()
                    manifest = {"format": "toolbox-legacy-inventory/v1", "source_sha256": digest, **dict(item["inventory"])}
                    connection.execute(
                        "INSERT INTO submission_records(id,project_slug,version_seq,version_label,state,author_id,author_display,title,description,artifact_ref,artifact_sha256,artifact_size,manifest_json,legacy,artifact_provider,created_at,updated_at) VALUES (?,?,?,?, 'submitted','legacy-import','Legacy import',?,?, ?,?,?,?,1,'legacy',?,?)",
                        (submission_id, item["project_slug"], version, f"v{version}", f"Legacy {Path(relative).name}", "Imported from the preserved legacy archive.", relative, digest, item["size"], json.dumps(manifest, sort_keys=True, separators=(",", ":")), timestamp, timestamp),
                    )
                    connection.execute("INSERT INTO submission_builds(id,submission_id,provider,status,started_at,finished_at) VALUES (?,?, 'legacy','not_run',?,?)", ("bld_" + secrets.token_hex(16), submission_id, timestamp, timestamp))
                    repo.append_event(submission_id, "legacy_imported", "legacy-import", {"source_sha256": digest})
                    connection.execute(
                        "INSERT INTO legacy_sources(id,relative_path,sha256,size,mtime_ns,file_id,project_slug,submission_id,imported_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("leg_" + secrets.token_hex(16), relative, digest, item["size"], item["mtime_ns"], item["file_id"], item["project_slug"], submission_id, datetime.now(timezone.utc).isoformat()),
                    )
                    imported += 1

                for source_path, opened, handle, file_id, item in retained_sources:
                    current_info = source_path.lstat()
                    if _is_reparse(current_info):
                        raise ValueError("legacy source changed during apply")
                    if os.name == "nt":
                        if (current_info.st_size, current_info.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                            raise ValueError("legacy source changed during apply")
                    elif not _same_path_identity(current_info, opened):
                        raise ValueError("legacy source changed during apply")
                    verified = _source_plan_item(str(item["relative_path"]), handle, opened, file_id)
                    if any(verified[field] != item[field] for field in comparison_fields):
                        raise ValueError("legacy source changed during apply")
    finally:
        connection.close()
    return {"plan_hash": plan_hash, "imported": imported, "observed": observed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or index preserved legacy submission ZIP files")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-hash")
    parser.add_argument("--root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps(build_plan(root=args.root, database_path=args.database), sort_keys=True))
        return 0
    if not args.plan_hash:
        parser.error("--apply requires --plan-hash")
    print(json.dumps(apply_plan(args.plan_hash, root=args.root, database_path=args.database), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
