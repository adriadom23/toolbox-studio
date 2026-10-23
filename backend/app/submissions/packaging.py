from __future__ import annotations

import hashlib
import json
import re
import stat
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.errors import PublicError
from app.projects.safe_fs import walk_regular_files_nofollow
from app.submissions.snapshot import SnapshotResult
from app.submissions.stores import sha256_file

_FIXED_DATE = (1980, 1, 1, 0, 0, 0)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PackageResult:
    path: Path
    size: int
    sha256: str
    manifest: dict[str, object]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.flag_bits |= 0x800
    return info


def create_reproducible_package(
    snapshot: SnapshotResult,
    destination: Path,
    *,
    submission_id: str,
    version: int,
    harness_sha256: str,
    git: dict[str, object] | None = None,
    lock_sha256: str | None = None,
    max_bytes: int = 100 * 1024 * 1024,
    timeout_seconds: float = 900,
) -> PackageResult:
    source_files = [item for item in snapshot.files if item.kind in {"frontend", "backend"}]
    manifest: dict[str, object] = {
        "format": "toolbox-submission-manifest/v2",
        "version": 2,
        "project": snapshot.project_slug,
        "submission_id": submission_id,
        "submission_version": version,
        "frontend_root": f"frontend/src/projects/{snapshot.project_slug}/",
        "backend_root": f"backend/app/projects/{snapshot.project_slug}/",
        "packaged_at": "1980-01-01T00:00:00+00:00",
        "input_sha256": snapshot.input_sha256,
        "harness_sha256": harness_sha256,
        "lock_sha256": lock_sha256,
        "policy_version": snapshot.policy_version,
        "git": git or {},
        "files": [{"path": item.path, "size": item.size, "sha256": item.sha256, "kind": item.kind} for item in source_files],
    }
    if sum(item.size for item in source_files) > max_bytes:
        raise ValueError("package source exceeds configured size limit")
    expected = {item.path: item for item in source_files}
    seen: set[str] = set()
    copied_bytes = 0
    deadline = time.monotonic() + max(float(timeout_seconds), 0.001)
    roots = (
        ("backend", snapshot.root / "backend" / "app" / "projects" / snapshot.project_slug),
        ("frontend", snapshot.root / "frontend" / "src" / "projects" / snapshot.project_slug),
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for kind, root in roots:
                for relative, source_file in walk_regular_files_nofollow(root):
                    stored_name = f"{kind}/{snapshot.project_slug}/{relative}"
                    item = expected.get(stored_name)
                    if item is None or item.kind != kind or stored_name in seen:
                        raise ValueError("sealed snapshot entries do not match the manifest")
                    if int(source_file.expected.st_size) != item.size:
                        raise ValueError("sealed snapshot entry changed before packaging")
                    digest = hashlib.sha256()
                    size = 0
                    with source_file.open() as (input_file, _opened), archive.open(_zip_info(stored_name), "w", force_zip64=True) as output_file:
                        while chunk := input_file.read(1024 * 1024):
                            if time.monotonic() > deadline:
                                raise PublicError("package_timeout", "package creation exceeded its time limit", 504, recoverable=True)
                            size += len(chunk)
                            copied_bytes += len(chunk)
                            if size > item.size or copied_bytes > max_bytes:
                                raise ValueError("package source exceeds configured size limit")
                            digest.update(chunk)
                            output_file.write(chunk)
                    if size != item.size or digest.hexdigest() != item.sha256:
                        raise ValueError("sealed snapshot entry integrity mismatch")
                    seen.add(stored_name)
            if seen != set(expected):
                raise ValueError("sealed snapshot entries do not match the manifest")
            if time.monotonic() > deadline:
                raise TimeoutError("package creation timed out")
            archive.writestr(_zip_info("MANIFEST.json"), _canonical_json(manifest), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    except OSError as exc:
        raise ValueError("sealed snapshot changed while packaging") from exc
    verify_package(destination, manifest)
    size, digest = sha256_file(destination)
    return PackageResult(destination, size, digest, manifest)


def verify_package(path: Path, expected_manifest: dict[str, object] | None = None) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not names or names[-1] != "MANIFEST.json":
            raise ValueError("package entries are not canonical")
        try:
            manifest = json.loads(archive.read("MANIFEST.json"))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("package manifest is invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != "toolbox-submission-manifest/v2" or manifest.get("version") != 2:
            raise ValueError("package manifest is invalid")
        project = manifest.get("project")
        files = manifest.get("files")
        if not isinstance(project, str) or not project or not isinstance(files, list):
            raise ValueError("package manifest is invalid")
        canonical_paths: set[str] = set()
        manifest_paths: list[str] = []
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256", "kind"}:
                raise ValueError("package manifest file is invalid")
            item_path = item.get("path")
            item_size = item.get("size")
            item_sha256 = item.get("sha256")
            item_kind = item.get("kind")
            if (
                not isinstance(item_path, str)
                or "\\" in item_path
                or "\x00" in item_path
                or not isinstance(item_size, int)
                or isinstance(item_size, bool)
                or item_size < 0
                or not isinstance(item_sha256, str)
                or not _SHA256_RE.fullmatch(item_sha256)
                or item_kind not in {"frontend", "backend"}
            ):
                raise ValueError("package manifest file is invalid")
            logical = PurePosixPath(item_path)
            expected_prefix = ("frontend", project) if item_kind == "frontend" else ("backend", project)
            if logical.is_absolute() or any(part in {"", ".", ".."} for part in item_path.split("/")) or logical.parts[:2] != expected_prefix:
                raise ValueError("package manifest path is invalid")
            semantic = unicodedata.normalize("NFC", item_path).casefold()
            if semantic in canonical_paths:
                raise ValueError("package manifest paths are not unique")
            canonical_paths.add(semantic)
            manifest_paths.append(item_path)
        expected_names = sorted(manifest_paths) + ["MANIFEST.json"]
        if manifest_paths != sorted(manifest_paths) or names != expected_names:
            raise ValueError("package entries do not exactly match the manifest")
        if expected_manifest is not None and _canonical_json(manifest) != _canonical_json(expected_manifest):
            raise ValueError("package manifest mismatch")
        for item in files:
            digest = hashlib.sha256()
            size = 0
            with archive.open(item["path"]) as handle:
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise ValueError("package entry integrity mismatch")
        return manifest
