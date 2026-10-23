from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.errors import PublicError
from app.projects.safe_fs import UnsafeFilesystemEntryError, is_safe_directory, stable_directories, walk_regular_files_nofollow
from app.projects.validation import validate_slug

_POLICY_VERSION = 2
_OMIT_DIRS = {"node_modules", "venv", ".venv", "__pycache__", ".pytest_cache", ".vite", "dist", ".git"}
_OMIT_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".zip"}
_SECRET_EXACT = {".npmrc", ".pypirc", "studio.config.json"}
_SECRET_PREFIX = (".env", "credentials", "id_rsa", "id_ed25519")
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der")


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    size: int
    sha256: str
    kind: str


@dataclass(frozen=True)
class SnapshotResult:
    root: Path
    project_slug: str
    files: tuple[SnapshotFile, ...]
    input_sha256: str
    policy_version: int = _POLICY_VERSION


def _forbidden(name: str) -> bool:
    folded = unicodedata.normalize("NFKC", name).casefold()
    return folded in _SECRET_EXACT or folded == ".aws" or folded.startswith(_SECRET_PREFIX) or folded.endswith(_SECRET_SUFFIXES)


def _omitted(name: str, is_dir: bool) -> bool:
    folded = name.casefold()
    return (is_dir and folded in _OMIT_DIRS) or (not is_dir and (folded.endswith(tuple(_OMIT_SUFFIXES)) or folded.startswith("~")))


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))


def descriptor_file_id(handle) -> str:
    if os.name != "nt":
        info = os.fstat(handle.fileno())
        return f"{info.st_dev}:{info.st_ino}"
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL
    result = ByHandleFileInformation()
    if not get_info(msvcrt.get_osfhandle(handle.fileno()), ctypes.byref(result)):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))
    if result.dwFileAttributes & 0x400:
        raise OSError("source is a reparse point")
    return f"{int(result.dwVolumeSerialNumber)}:{int(result.nFileIndexHigh)}:{int(result.nFileIndexLow)}"


@contextmanager
def _open_regular_nofollow(path: Path, *, deny_write: bool = False):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        share_mode = 0x00000001 if deny_write else 0x00000001 | 0x00000002 | 0x00000004
        handle = create_file(
            str(path), 0x80000000, share_mode,
            None, 3, 0x00200000 | 0x02000000, None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error), str(path))
        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        info = ByHandleFileInformation()
        get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandle
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, ctypes.byref(info)) or info.dwFileAttributes & 0x400:
            error = ctypes.get_last_error()
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
            raise OSError(error or 22, "source is a reparse point", str(path))
        descriptor = msvcrt.open_osfhandle(int(handle), flags)
    else:
        descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as file_handle:
        opened = os.fstat(file_handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("source is not a regular file")
        if deny_write and os.name != "nt":
            import fcntl
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_SH)
        yield file_handle, opened


class SafeSnapshotBuilder:
    def __init__(self, frontend_projects: Path, backend_projects: Path, shared_root: Path | None = None, *, max_files: int = 10_000, max_bytes: int = 100 * 1024 * 1024, max_depth: int = 64, extra_exclusions: tuple[str, ...] = ()) -> None:
        self.frontend_projects = Path(frontend_projects)
        self.backend_projects = Path(backend_projects)
        self.shared_root = Path(shared_root) if shared_root else self.frontend_projects.parent / "shared"
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.max_depth = max_depth
        self.extra_exclusions = {item.casefold() for item in extra_exclusions}

    def _copy_tree(self, source: Path, destination: Path, logical_prefix: str, kind: str, collected: list[SnapshotFile]) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        total = sum(item.size for item in collected)

        def should_skip(relative: str, is_dir: bool) -> bool:
            parts = PurePosixPath(relative).parts
            normalized_parts = tuple(unicodedata.normalize("NFC", part) for part in parts)
            normalized = normalized_parts[-1]
            logical = f"{logical_prefix}/{PurePosixPath(*normalized_parts).as_posix()}"
            if (is_dir and len(parts) > self.max_depth) or (not is_dir and len(parts) > self.max_depth + 1):
                raise PublicError("snapshot_limit", "snapshot depth limit exceeded", 413)
            if _forbidden(normalized):
                raise PublicError("forbidden_source_file", logical, 422)
            if _omitted(normalized, is_dir) or normalized.casefold() in self.extra_exclusions:
                return True
            collision = logical.casefold()
            if collision in seen:
                raise PublicError("source_collision", logical, 422)
            seen.add(collision)
            return False

        try:
            for relative, source_file in walk_regular_files_nofollow(source, should_skip=should_skip):
                normalized_parts = tuple(unicodedata.normalize("NFC", part) for part in PurePosixPath(relative).parts)
                normalized_relative = PurePosixPath(*normalized_parts).as_posix()
                logical = f"{logical_prefix}/{normalized_relative}"
                expected_size = int(source_file.expected.st_size)
                if len(collected) + 1 > self.max_files or total + expected_size > self.max_bytes:
                    raise PublicError("snapshot_limit", "snapshot size limit exceeded", 413)
                digest = hashlib.sha256()
                size = 0
                output = destination.joinpath(*normalized_parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with source_file.open() as (input_file, _opened_stat), output.open("xb") as output_file:
                        while chunk := input_file.read(1024 * 1024):
                            size += len(chunk)
                            digest.update(chunk)
                            output_file.write(chunk)
                except PublicError:
                    raise
                except OSError as exc:
                    raise PublicError("source_changed", logical, 409) from exc
                if size != expected_size:
                    raise PublicError("source_changed", logical, 409)
                total += size
                collected.append(SnapshotFile(logical, size, digest.hexdigest(), kind))
        except UnsafeFilesystemEntryError as exc:
            relative = PurePosixPath(exc.relative).as_posix()
            raise PublicError("forbidden_source_file", f"{logical_prefix}/{relative}", 422) from exc
        except PublicError:
            raise
        except OSError as exc:
            raise PublicError("source_changed", logical_prefix, 409) from exc

    def build(self, project_slug: str, destination: Path) -> SnapshotResult:
        slug = validate_slug(project_slug)
        frontend = self.frontend_projects / slug
        backend = self.backend_projects / slug
        if not is_safe_directory(frontend) or not is_safe_directory(backend):
            raise PublicError("not_found", "project not found", 404)
        try:
            with stable_directories(frontend, backend):
                return self._build_stable(slug, frontend, backend, destination)
        except PublicError:
            raise
        except OSError as exc:
            raise PublicError("source_changed", "project root changed during snapshot", 409) from exc

    def _build_stable(self, slug: str, frontend: Path, backend: Path, destination: Path) -> SnapshotResult:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=False)
        files: list[SnapshotFile] = []
        self._copy_tree(frontend, root / "frontend" / "src" / "projects" / slug, f"frontend/{slug}", "frontend", files)
        self._copy_tree(backend, root / "backend" / "app" / "projects" / slug, f"backend/{slug}", "backend", files)
        if self.shared_root.is_dir() and not self.shared_root.is_symlink():
            self._copy_tree(self.shared_root, root / "frontend" / "src" / "shared", "administrative/shared", "administrative", files)
        for package in (root / "backend" / "app" / "__init__.py", root / "backend" / "app" / "projects" / "__init__.py"):
            if not package.exists():
                package.parent.mkdir(parents=True, exist_ok=True)
                package.write_text("", encoding="utf-8")
        canonical = "\n".join(f"{item.path}\0{item.size}\0{item.sha256}\0{item.kind}" for item in sorted(files, key=lambda item: item.path))
        digest = hashlib.sha256(f"policy:{_POLICY_VERSION}\n{canonical}".encode("utf-8")).hexdigest()
        return SnapshotResult(root, slug, tuple(sorted(files, key=lambda item: item.path)), digest)


def snapshot_manifest_json(snapshot: SnapshotResult) -> str:
    payload = {
        "policy_version": snapshot.policy_version,
        "project_slug": snapshot.project_slug,
        "files": [
            {"path": item.path, "size": item.size, "sha256": item.sha256, "kind": item.kind}
            for item in snapshot.files
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _archive_logical_path(relative: str, slug: str) -> tuple[str, str] | None:
    prefixes = (
        (f"frontend/src/projects/{slug}/", f"frontend/{slug}/", "frontend"),
        (f"backend/app/projects/{slug}/", f"backend/{slug}/", "backend"),
        ("frontend/src/shared/", "administrative/shared/", "administrative"),
    )
    for archive_prefix, logical_prefix, kind in prefixes:
        if relative.startswith(archive_prefix) and len(relative) > len(archive_prefix):
            return logical_prefix + relative[len(archive_prefix):], kind
    return None


def write_verified_snapshot_zip(
    root: Path,
    destination: Path,
    project_slug: str,
    expected_input_sha256: str,
) -> tuple[int, str]:
    """Stream a sealed snapshot into a ZIP while retaining and verifying its parents."""

    slug = validate_slug(project_slug)
    if len(expected_input_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_input_sha256):
        raise ValueError("sealed snapshot digest is invalid")
    synthetic = {"backend/app/__init__.py", "backend/app/projects/__init__.py"}
    synthetic_seen: set[str] = set()
    files: list[SnapshotFile] = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, source_file in walk_regular_files_nofollow(root):
                if unicodedata.normalize("NFC", relative) != relative:
                    raise ValueError("sealed snapshot contains a non-canonical path")
                mapped = _archive_logical_path(relative, slug)
                if mapped is None and relative not in synthetic:
                    raise ValueError("sealed snapshot contains an unexpected file")
                info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                digest = hashlib.sha256()
                size = 0
                with source_file.open() as (input_file, _opened), archive.open(info, "w") as output_file:
                    while chunk := input_file.read(1024 * 1024):
                        size += len(chunk)
                        digest.update(chunk)
                        output_file.write(chunk)
                file_sha256 = digest.hexdigest()
                if relative in synthetic:
                    if size != 0 or file_sha256 != hashlib.sha256(b"").hexdigest():
                        raise ValueError("sealed snapshot package marker changed")
                    synthetic_seen.add(relative)
                else:
                    logical, kind = mapped
                    files.append(SnapshotFile(logical, size, file_sha256, kind))
    except OSError as exc:
        raise ValueError("sealed snapshot changed while creating remote input") from exc
    if synthetic_seen != synthetic:
        raise ValueError("sealed snapshot package markers are missing")
    actual_sorted = tuple(sorted(files, key=lambda item: item.path))
    canonical = "\n".join(
        f"{item.path}\0{item.size}\0{item.sha256}\0{item.kind}"
        for item in actual_sorted
    )
    actual_input_sha256 = hashlib.sha256(f"policy:{_POLICY_VERSION}\n{canonical}".encode("utf-8")).hexdigest()
    if actual_input_sha256 != expected_input_sha256:
        raise ValueError("sealed snapshot files do not match the expected digest")
    digest = hashlib.sha256()
    size = 0
    with _open_regular_nofollow(destination, deny_write=True) as (handle, _opened):
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _scan_sealed_tree(root: Path, logical_prefix: str, kind: str) -> list[SnapshotFile]:
    files: list[SnapshotFile] = []
    try:
        for relative, source_file in walk_regular_files_nofollow(root):
            if unicodedata.normalize("NFC", relative) != relative:
                raise ValueError("sealed snapshot contains a non-canonical path")
            logical = f"{logical_prefix}/{relative}"
            digest = hashlib.sha256()
            size = 0
            with source_file.open() as (handle, _opened):
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            files.append(SnapshotFile(logical, size, digest.hexdigest(), kind))
    except OSError as exc:
        raise ValueError("sealed snapshot contains a link, reparse point, or changed entry") from exc
    return files


def load_sealed_snapshot(
    root: Path,
    project_slug: str,
    manifest_json: str,
    expected_input_sha256: str,
    *,
    max_files: int = 10_000,
    max_bytes: int = 100 * 1024 * 1024,
) -> SnapshotResult:
    slug = validate_slug(project_slug)
    try:
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("sealed snapshot manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"policy_version", "project_slug", "files"}:
        raise ValueError("sealed snapshot manifest is invalid")
    if manifest["policy_version"] != _POLICY_VERSION or manifest["project_slug"] != slug or not isinstance(manifest["files"], list):
        raise ValueError("sealed snapshot manifest does not match the submission")
    expected: list[SnapshotFile] = []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256", "kind"}:
            raise ValueError("sealed snapshot manifest file is invalid")
        if (
            not isinstance(item["path"], str)
            or not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in item["sha256"])
            or item["kind"] not in {"frontend", "backend", "administrative"}
        ):
            raise ValueError("sealed snapshot manifest file is invalid")
        expected.append(SnapshotFile(item["path"], item["size"], item["sha256"], item["kind"]))
    actual = []
    roots = (
        (Path(root) / "frontend" / "src" / "projects" / slug, f"frontend/{slug}", "frontend"),
        (Path(root) / "backend" / "app" / "projects" / slug, f"backend/{slug}", "backend"),
    )
    for source, prefix, kind in roots:
        actual.extend(_scan_sealed_tree(source, prefix, kind))
    shared = Path(root) / "frontend" / "src" / "shared"
    if shared.exists():
        actual.extend(_scan_sealed_tree(shared, "administrative/shared", "administrative"))
    actual_sorted = tuple(sorted(actual, key=lambda item: item.path))
    expected_sorted = tuple(sorted(expected, key=lambda item: item.path))
    if actual_sorted != expected_sorted:
        raise ValueError("sealed snapshot files do not match the durable manifest")
    if len(actual_sorted) > max_files or sum(item.size for item in actual_sorted) > max_bytes:
        raise ValueError("sealed snapshot exceeds configured limits")
    canonical = "\n".join(
        f"{item.path}\0{item.size}\0{item.sha256}\0{item.kind}"
        for item in actual_sorted
    )
    digest = hashlib.sha256(f"policy:{_POLICY_VERSION}\n{canonical}".encode("utf-8")).hexdigest()
    if digest != expected_input_sha256:
        raise ValueError("sealed snapshot digest does not match the submission")
    return SnapshotResult(Path(root), slug, actual_sorted, digest)
