from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class ObjectRef:
    provider: str
    key: str


@dataclass(frozen=True)
class ObjectStat:
    size: int
    sha256: str


@dataclass(frozen=True)
class DownloadSpec:
    kind: str
    url: str | None = None
    ttl_seconds: int | None = None


@dataclass(frozen=True)
class PreflightResult:
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class BuildRequest:
    project_slug: str
    snapshot_root: Path
    output_root: Path
    timeout_seconds: int = 900
    input_sha256: str | None = None
    marker: str | None = None
    cancellation_event: object | None = None


@dataclass(frozen=True)
class BuildHandle:
    id: str
    marker: str | None = None


@dataclass(frozen=True)
class BuildPoll:
    status: str
    error_code: str | None = None


@dataclass(frozen=True)
class BuildOutputs:
    ok: bool
    harness_sha256: str
    phases: tuple[dict[str, object], ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class AppendResult:
    bytes_written: int
    truncated: bool = False


@dataclass(frozen=True)
class LogPage:
    lines: tuple[str, ...]
    next_cursor: str | None
    has_more: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    provider_id: str | None = None
    state: str = "delivered"


@dataclass(frozen=True)
class PublicationResult:
    ok: bool
    reference: str | None = None


@dataclass(frozen=True)
class NotificationEnvelope:
    project: str
    version: str
    short_id: str
    author: str
    state: str
    review_url: str


@dataclass(frozen=True)
class GitMetadata:
    commit: str | None = None
    branch: str | None = None
    remote: str | None = None
    dirty: bool | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


class ArtifactStore(Protocol):
    def put_if_absent(self, object_id: str, source: Path, size: int, sha256: str, metadata: dict[str, str] | None = None) -> ObjectRef: ...
    def resolve_existing(self, object_id: str, size: int, sha256: str) -> ObjectRef: ...
    def stat(self, ref: ObjectRef) -> ObjectStat: ...
    def open_stream(self, ref: ObjectRef) -> AbstractContextManager[BinaryIO]: ...
    def create_download(self, ref: ObjectRef, filename: str, ttl_seconds: int) -> DownloadSpec: ...


class BuildProvider(Protocol):
    def preflight(self) -> PreflightResult: ...
    def prepare_input(self, request: BuildRequest) -> dict[str, str]: ...
    def start(self, request: BuildRequest, expected: dict[str, str] | None = None) -> BuildHandle: ...
    def reconcile(self, marker: str, window_seconds: int) -> list[BuildHandle]: ...
    def poll(self, handle: BuildHandle) -> BuildPoll: ...
    def fetch_outputs(self, handle: BuildHandle, expected: dict[str, str]) -> BuildOutputs: ...


class LogStore(Protocol):
    def append(self, ref: str, phase: str, safe_chunk: str) -> AppendResult: ...
    def read_page(self, ref: str, phase: str, cursor: str | None, line_limit: int, byte_limit: int) -> LogPage: ...


class NotificationProvider(Protocol):
    def send(self, effect_id: str, generation: int, envelope: NotificationEnvelope) -> ProviderResult: ...


class PublicationProvider(Protocol):
    def publish(self, effect_id: str, generation: int, submission: dict[str, object], target: str) -> PublicationResult: ...


class GitMetadataProvider(Protocol):
    def inspect(self, checkout_root: Path, timeout_seconds: int = 5) -> GitMetadata: ...
