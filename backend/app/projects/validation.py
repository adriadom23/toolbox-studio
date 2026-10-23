from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
MAX_SLUG_LEN = 40
_RESERVED_WINDOWS_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class InvalidProjectNameError(ValueError):
    pass


def _validate(slug: str) -> str:
    if (
        len(slug) > MAX_SLUG_LEN
        or not SLUG_RE.fullmatch(slug)
        or slug.casefold() in _RESERVED_WINDOWS_NAMES
    ):
        raise InvalidProjectNameError("invalid project slug")
    return slug


def slugify(raw_name: str) -> str:
    text = str(raw_name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:MAX_SLUG_LEN]
    try:
        return _validate(slug)
    except InvalidProjectNameError as exc:
        raise InvalidProjectNameError(
            "Project name must produce a safe slug matching ^[a-z][a-z0-9-]*$ "
            "(letters/digits/hyphens, must start with a letter, and must not be a Windows device name)."
        ) from exc


def validate_slug(slug: str) -> str:
    return _validate(slug)


def validate_project_file_path(value: str) -> tuple[str, str, str]:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 240
        or value != unicodedata.normalize("NFC", value)
        or "\\" in value
        or "%" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid project file path")
    segments = value.split("/")
    if len(segments) < 2 or segments[0] not in {"frontend", "backend"}:
        raise ValueError("invalid project file path")
    if any(
        not segment
        or segment in {".", ".."}
        or segment[-1] in {".", " "}
        or any(character in '<>:"|?*' for character in segment)
        or segment.split(".", 1)[0].casefold() in _RESERVED_WINDOWS_NAMES
        for segment in segments
    ):
        raise ValueError("invalid project file path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError("invalid project file path")
    prefix = segments[0]
    child = "/".join(segments[1:])
    if segments[-1].casefold() == "project.meta.json":
        raise ValueError("reserved project file path")
    collision_key = unicodedata.normalize("NFKC", value).casefold()
    return prefix, child, collision_key
