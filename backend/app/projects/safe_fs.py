import hashlib
import json
import os
import stat
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class UnsafeFilesystemEntryError(OSError):
    def __init__(self, relative: str) -> None:
        super().__init__("project tree contains a link, reparse point, or non-regular entry")
        self.relative = relative


def _unsafe_directory(info: os.stat_result) -> bool:
    return (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _unsafe_file(info: os.stat_result) -> bool:
    return (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
    )


@contextmanager
def _open_directory_nofollow_handle(
    path: Path,
    *,
    parent_descriptor: int | None = None,
    child_name: str | None = None,
) -> Iterator[tuple[tuple[int, ...], int | None]]:
    """Open a directory itself and retain its identity and POSIX descriptor."""

    candidate = Path(path)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        before = candidate.lstat()
        if _unsafe_directory(before):
            raise OSError("project directory is not a regular directory")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(candidate),
            0x80000000,
            0x00000001 | 0x00000002,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error), str(candidate))

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
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, ctypes.byref(info)) or info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error or 22, "project directory is a reparse point", str(candidate))
        identity = (
            int(info.dwVolumeSerialNumber),
            int(info.nFileIndexHigh),
            int(info.nFileIndexLow),
        )
        try:
            yield identity, None
        finally:
            kernel32.CloseHandle(handle)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if parent_descriptor is None:
        descriptor = os.open(candidate, flags)
    else:
        descriptor = os.open(str(child_name), flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if _unsafe_directory(opened):
            raise OSError("project directory is not a regular directory")
        yield (int(opened.st_dev), int(opened.st_ino)), descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_directory_nofollow(path: Path) -> Iterator[tuple[int, ...]]:
    with _open_directory_nofollow_handle(path) as (identity, _descriptor):
        yield identity


def is_safe_directory(path: Path) -> bool:
    try:
        with _open_directory_nofollow(path):
            return True
    except OSError:
        return False


@contextmanager
def stable_directories(*paths: Path) -> Iterator[None]:
    """Reject links/reparse roots and detect identity replacement while in use."""

    candidates = tuple(Path(path) for path in paths)
    with ExitStack() as stack:
        identities = tuple(stack.enter_context(_open_directory_nofollow(path)) for path in candidates)
        yield
        for path, expected in zip(candidates, identities, strict=True):
            with _open_directory_nofollow(path) as current:
                if current != expected:
                    raise OSError("project root identity changed")


def _relative_parts(relative: str) -> tuple[str, ...]:
    if not relative or "\x00" in relative or "\\" in relative:
        raise ValueError("invalid path")
    parsed = PurePosixPath(relative)
    parts = parsed.parts
    if parsed.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid path")
    return parts


def _child_lstat(parent: Path, parent_descriptor: int | None, name: str) -> os.stat_result:
    if os.name != "nt" and parent_descriptor is not None:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    return (parent / name).lstat()


def _mkdir_child(parent: Path, parent_descriptor: int | None, name: str) -> None:
    if os.name != "nt" and parent_descriptor is not None:
        os.mkdir(name, dir_fd=parent_descriptor)
    else:
        (parent / name).mkdir()


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))


@dataclass(frozen=True)
class StableRegularFile:
    path: Path
    name: str
    parent_descriptor: int | None
    expected: os.stat_result

    @contextmanager
    def open(self):
        if os.name != "nt" and self.parent_descriptor is not None:
            descriptor = os.open(
                self.name,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self.parent_descriptor,
            )
            opened_context = None
            handle = os.fdopen(descriptor, "rb")
        else:
            from app.submissions.snapshot import _open_regular_nofollow

            opened_context = _open_regular_nofollow(self.path)
            handle, opened = opened_context.__enter__()
        try:
            if opened_context is None:
                opened = os.fstat(handle.fileno())
            if _unsafe_file(opened) or _file_identity(opened) != _file_identity(self.expected):
                raise OSError("project file changed while opening")
            yield handle, opened
            if _file_identity(os.fstat(handle.fileno())) != _file_identity(opened):
                raise OSError("project file changed while reading")
        finally:
            if opened_context is None:
                handle.close()
            else:
                opened_context.__exit__(None, None, None)


def walk_regular_files_nofollow(
    root: Path,
    *,
    should_skip: Callable[[str, bool], bool] | None = None,
) -> Iterator[tuple[str, StableRegularFile]]:
    """Walk regular files while retaining every no-follow parent directory."""

    candidate = Path(root).absolute()

    def visit(current: Path, descriptor: int | None, parts: tuple[str, ...]):
        scan_target = descriptor if os.name != "nt" and descriptor is not None else current
        with os.scandir(scan_target) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            info = _child_lstat(current, descriptor, name)
            child = current / name
            child_parts = (*parts, name)
            relative = PurePosixPath(*child_parts).as_posix()
            if stat.S_ISDIR(info.st_mode) and not _unsafe_directory(info):
                if should_skip is not None and should_skip(relative, True):
                    continue
                with _open_directory_nofollow_handle(
                    child,
                    parent_descriptor=descriptor,
                    child_name=name,
                ) as (identity, child_descriptor):
                    yield from visit(child, child_descriptor, child_parts)
                    with _open_directory_nofollow(child) as current_identity:
                        if current_identity != identity:
                            raise OSError("project directory identity changed")
                continue
            if _unsafe_file(info):
                raise UnsafeFilesystemEntryError(relative)
            if should_skip is not None and should_skip(relative, False):
                continue
            yield relative, StableRegularFile(child, name, descriptor, info)

    with _open_directory_nofollow_handle(candidate) as (identity, descriptor):
        yield from visit(candidate, descriptor, ())
        with _open_directory_nofollow(candidate) as current_identity:
            if current_identity != identity:
                raise OSError("project root identity changed")


@dataclass(frozen=True)
class StableFileTarget:
    path: Path
    name: str
    parent_descriptor: int | None

    def sha256(self, *, normalize_text: bool = False) -> str | None:
        try:
            if os.name != "nt" and self.parent_descriptor is not None:
                descriptor = os.open(
                    self.name,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self.parent_descriptor,
                )
                handle_context = os.fdopen(descriptor, "rb")
                opened_context = None
            else:
                from app.submissions.snapshot import _open_regular_nofollow

                opened_context = _open_regular_nofollow(self.path)
                handle_context, _opened = opened_context.__enter__()
            try:
                payload = handle_context.read()
            finally:
                if opened_context is None:
                    handle_context.close()
                else:
                    opened_context.__exit__(None, None, None)
        except FileNotFoundError:
            return None
        if normalize_text:
            try:
                payload = payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            except UnicodeDecodeError:
                return None
        return hashlib.sha256(payload).hexdigest()

    def replace_from(self, source: Path) -> None:
        if os.name != "nt" and self.parent_descriptor is not None:
            os.replace(source, self.name, dst_dir_fd=self.parent_descriptor)
        else:
            os.replace(source, self.path)
        info = _child_lstat(self.path.parent, self.parent_descriptor, self.name)
        if _unsafe_file(info):
            raise OSError("promoted project file is not regular")


@contextmanager
def stable_file_target(base: Path, relative: str, *, create_parents: bool = False) -> Iterator[StableFileTarget]:
    """Retain every no-follow parent while a project file is checked/promoted."""

    parts = _relative_parts(relative)
    root = Path(base).absolute()
    paths: list[Path] = [root]
    identities: list[tuple[int, ...]] = []
    with ExitStack() as stack:
        identity, descriptor = stack.enter_context(_open_directory_nofollow_handle(root))
        identities.append(identity)
        current = root
        for part in parts[:-1]:
            try:
                info = _child_lstat(current, descriptor, part)
            except FileNotFoundError:
                if not create_parents:
                    raise
                try:
                    _mkdir_child(current, descriptor, part)
                except FileExistsError:
                    pass
                info = _child_lstat(current, descriptor, part)
            if _unsafe_directory(info):
                raise OSError("project path contains a link or reparse point")
            current = current / part
            paths.append(current)
            identity, descriptor = stack.enter_context(
                _open_directory_nofollow_handle(
                    current,
                    parent_descriptor=descriptor,
                    child_name=part,
                )
            )
            identities.append(identity)
        try:
            target_info = _child_lstat(current, descriptor, parts[-1])
        except FileNotFoundError:
            target_info = None
        if target_info is not None and _unsafe_file(target_info):
            raise OSError("project target is not a regular file")
        yield StableFileTarget(current / parts[-1], parts[-1], descriptor)
        try:
            target_info = _child_lstat(current, descriptor, parts[-1])
        except FileNotFoundError:
            target_info = None
        if target_info is not None and _unsafe_file(target_info):
            raise OSError("project target changed to a non-regular file")
        for path, expected in zip(paths, identities, strict=True):
            with _open_directory_nofollow(path) as current_identity:
                if current_identity != expected:
                    raise OSError("project path parent identity changed")


def resolve_within(base: Path, relative: str) -> Path:
    """Resolve lexically while rejecting links/reparse points in every component."""

    parts = _relative_parts(relative)
    root = Path(base).absolute()
    with stable_directories(root):
        current = root
        for part in parts[:-1]:
            candidate = current / part
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                return root.joinpath(*parts)
            if _unsafe_directory(info):
                raise ValueError("path contains a link or reparse point")
            current = candidate
        target = current / parts[-1]
        try:
            info = target.lstat()
        except FileNotFoundError:
            return target
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("path contains a link or reparse point")
        return target


def replace_directory_child(root: Path, source_name: str, target_name: str) -> None:
    """Rename immediate children through the retained administrative root."""

    if any(name in {"", ".", ".."} or Path(name).name != name for name in (source_name, target_name)):
        raise ValueError("invalid directory child name")
    candidate = Path(root).absolute()
    with _open_directory_nofollow_handle(candidate) as (identity, descriptor):
        if os.name != "nt" and descriptor is not None:
            os.replace(source_name, target_name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        else:
            os.replace(candidate / source_name, candidate / target_name)
        with _open_directory_nofollow(candidate) as current:
            if current != identity:
                raise OSError("administrative root identity changed")


def write_journal(path: Path, operation: str, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"operation": operation, "entries": entries, "created_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def quarantine_path(path: Path) -> Path | None:
    if not path.exists():
        return None
    prefix = path.name if path.name.startswith(".") else f".{path.name}"
    target = path.with_name(f"{prefix}.quarantine-{uuid.uuid4().hex}")
    os.replace(path, target)
    return target
