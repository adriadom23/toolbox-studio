from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from app.projects.safe_fs import stable_file_target
from app.submissions.providers import DownloadSpec, ObjectRef, ObjectStat
from app.submissions.snapshot import _open_regular_nofollow, descriptor_file_id


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _descriptor_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@contextmanager
def _open_verified_path(path: Path, *, expected_size: int | None = None, expected_sha256: str | None = None) -> Iterator[tuple[BinaryIO, ObjectStat]]:
    with _open_regular_nofollow(path, deny_write=True) as (handle, before):
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
        actual = ObjectStat(size, digest.hexdigest())
        if (
            not stat.S_ISREG(before.st_mode)
            or _descriptor_identity(before) != _descriptor_identity(after)
            or (expected_size is not None and actual.size != expected_size)
            or (expected_sha256 is not None and actual.sha256 != expected_sha256)
        ):
            raise ValueError("artifact integrity mismatch")
        handle.seek(0)
        yield handle, actual


class LegacyArtifactStore:
    provider = "legacy"

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, relative_path: str) -> Path:
        if not relative_path or "\\" in relative_path or "\x00" in relative_path:
            raise ValueError("legacy path is invalid")
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.split("/")):
            raise ValueError("legacy path escapes root")
        candidate = self.root.joinpath(*relative.parts)
        parent = candidate.parent.resolve(strict=True)
        if parent != self.root and self.root not in parent.parents:
            raise ValueError("legacy path escapes root")
        return candidate

    @contextmanager
    def open_source(self, relative_path: str, *, file_id: str, size: int, mtime_ns: int, sha256: str) -> Iterator[BinaryIO]:
        path = self._path(relative_path)
        with stable_file_target(self.root, relative_path) as target:
            if target.path != path:
                raise ValueError("legacy source integrity mismatch")
            with _open_verified_path(path, expected_size=size, expected_sha256=sha256) as (handle, _actual):
                info = os.fstat(handle.fileno())
                current_ids = {descriptor_file_id(handle), f"{info.st_dev}:{info.st_ino}"}
                if file_id not in current_ids or (info.st_size, info.st_mtime_ns) != (size, mtime_ns):
                    raise ValueError("legacy source integrity mismatch")
                yield handle

    def stat_source(self, relative_path: str, *, file_id: str, size: int, mtime_ns: int, sha256: str) -> ObjectStat:
        with self.open_source(relative_path, file_id=file_id, size=size, mtime_ns=mtime_ns, sha256=sha256):
            return ObjectStat(size, sha256)


class FilesystemArtifactStore:
    provider = "filesystem"

    def __init__(self, root: Path, kind: str = "artifacts", extension: str = ".zip") -> None:
        self.root = Path(root)
        self.kind = kind
        self.extension = extension
        self.root.mkdir(parents=True, exist_ok=True)
        info = self.root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("artifact root must be a regular directory")

    def _key(self, object_id: str) -> str:
        return hashlib.sha256(f"{self.kind}\0{object_id}".encode("utf-8")).hexdigest()

    def _path(self, object_id: str) -> Path:
        key = self._key(object_id)
        return self.root / key[:2] / key[2:4] / f"{key}{self.extension}"

    def put_if_absent(self, object_id: str, source: Path, size: int, sha256: str, metadata: dict[str, str] | None = None) -> ObjectRef:
        del metadata
        source = Path(source)
        source_info = source.lstat()
        if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISREG(source_info.st_mode):
            raise ValueError("artifact source must be a regular file")
        actual_size, actual_sha = sha256_file(source)
        if (actual_size, actual_sha) != (size, sha256):
            raise ValueError("artifact source integrity mismatch")
        target = self._path(object_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = self.stat(ObjectRef(self.provider, object_id))
            if (existing.size, existing.sha256) != (size, sha256):
                raise FileExistsError("artifact object collision")
            os.chmod(target, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            return ObjectRef(self.provider, object_id)

        staging = target.with_name(f".{target.name}.staging-{os.getpid()}-{os.urandom(8).hex()}")
        with staging.open("xb") as output, source.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        staged_size, staged_sha = sha256_file(staging)
        if (staged_size, staged_sha) != (size, sha256):
            raise ValueError("staged artifact integrity mismatch")
        collided = False
        try:
            # Hard-link creation is create-if-absent on both POSIX and Windows:
            # unlike rename(), it never replaces an existing destination.
            os.link(staging, target)
        except FileExistsError:
            collided = True
        retained_kind = "quarantine" if collided else "promoted"
        retained = target.with_name(f".{target.name}.{retained_kind}-{os.getpid()}-{os.urandom(8).hex()}")
        os.rename(staging, retained)
        if collided:
            existing = self.stat(ObjectRef(self.provider, object_id))
            if (existing.size, existing.sha256) != (size, sha256):
                raise FileExistsError("artifact object collision") from None
        os.chmod(target, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(retained, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        promoted = self.stat(ObjectRef(self.provider, object_id))
        if (promoted.size, promoted.sha256) != (size, sha256):
            raise ValueError("promoted artifact integrity mismatch")
        return ObjectRef(self.provider, object_id)

    def resolve_existing(self, object_id: str, size: int, sha256: str) -> ObjectRef:
        ref = ObjectRef(self.provider, object_id)
        current = self.stat(ref)
        if (current.size, current.sha256) != (size, sha256):
            raise FileExistsError("artifact object collision")
        return ref

    def stat(self, ref: ObjectRef) -> ObjectStat:
        if ref.provider != self.provider:
            raise ValueError("wrong artifact provider")
        with _open_verified_path(self._path(ref.key)) as (_handle, actual):
            return actual

    @contextmanager
    def open_verified(self, ref: ObjectRef, *, size: int, sha256: str) -> Iterator[tuple[BinaryIO, ObjectStat]]:
        if ref.provider != self.provider:
            raise ValueError("wrong artifact provider")
        with _open_verified_path(self._path(ref.key), expected_size=size, expected_sha256=sha256) as opened:
            yield opened

    @contextmanager
    def open_stream(self, ref: ObjectRef) -> Iterator[BinaryIO]:
        if ref.provider != self.provider:
            raise ValueError("wrong artifact provider")
        with _open_verified_path(self._path(ref.key)) as (handle, _actual):
            yield handle

    def create_download(self, ref: ObjectRef, filename: str, ttl_seconds: int = 900) -> DownloadSpec:
        del filename, ttl_seconds
        if ref.provider != self.provider:
            raise ValueError("wrong artifact provider")
        return DownloadSpec(kind="stream")
