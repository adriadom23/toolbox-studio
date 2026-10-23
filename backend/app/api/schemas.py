from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.projects.validation import InvalidProjectNameError, validate_slug

PROJECT_FILE_MAX_BYTES = 2 * 1024 * 1024
PROJECT_BATCH_MAX_BYTES = 10 * 1024 * 1024


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must contain non-whitespace characters")
        return normalized


class EditableFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["editable"] = "editable"
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    content: str


class OpaqueFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["opaque"] = "opaque"
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class ProjectFileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=PROJECT_FILE_MAX_BYTES)
    base_sha256: str | None


class ProjectFilesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[ProjectFileWrite] = Field(min_length=1, max_length=100)


class ProjectMembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    expected_version: int = Field(ge=0)


class ProjectClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str
    title: str = Field(default="Entrega", min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    predecessor_id: str | None = None

    @field_validator("title", "description")
    @classmethod
    def validate_trimmed_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must contain non-whitespace characters")
        return normalized


class LegacySubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=4000)
    title: str | None = Field(default=None, max_length=200)
    submitted_by: str | None = Field(default=None, max_length=200)
    locale: str | None = Field(default=None, max_length=16)


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    expected_row_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=4000)
    publication_target: str = "none"


class PublicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_row_version: int = Field(ge=1)
    target: str = "local-index"


class EffectRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)


class PreviewGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_kind: Literal["submission", "workspace"]
    subject_id: str = Field(min_length=1, max_length=44)
    preview_id: str | None = Field(default=None, pattern=r"^prv_[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_subject_id(self):
        if self.subject_kind == "submission":
            if not re.fullmatch(r"sub_[0-9a-f]{32}", self.subject_id):
                raise ValueError("invalid submission id")
        else:
            try:
                validate_slug(self.subject_id)
            except InvalidProjectNameError as exc:
                raise ValueError("invalid project slug") from exc
        return self
