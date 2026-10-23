from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectRecord:
    slug: str
    display_name: str
    created_at: str | None
    modified_at: str | None
    status: str
    version: int
    source: str = "filesystem"


@dataclass(frozen=True)
class SubmissionRecord:
    id: int
    project_slug: str
    status: str
    package_path: str | None
    package_sha256: str | None
    created_by: str
    created_at: str
    version: int
