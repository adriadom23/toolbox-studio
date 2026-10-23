from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.submissions.database import connect, transaction

_PREFIX = "runtime:v1:"
_REFERENCE_COLUMNS = (
    ("submission_records", "snapshot_ref"),
    ("submission_records", "build_output_ref"),
    ("preview_build_operations", "snapshot_ref"),
    ("preview_build_operations", "output_ref"),
    ("preview_objects", "runtime_ref"),
)


def _relative_parts(value: str) -> tuple[str, ...]:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("runtime reference is invalid")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("runtime reference is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute():
        raise ValueError("runtime reference is invalid")
    return relative.parts


def encode_runtime_ref(runtime_root: Path, path: Path) -> str:
    root = Path(runtime_root).resolve(strict=True)
    candidate = Path(path).resolve(strict=False)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("runtime reference escapes configured root") from exc
    parts = _relative_parts(relative.as_posix())
    return _PREFIX + PurePosixPath(*parts).as_posix()


def resolve_runtime_ref(runtime_root: Path, reference: str) -> Path:
    if not reference.startswith(_PREFIX):
        raise ValueError("runtime reference format is invalid")
    root = Path(runtime_root).resolve(strict=True)
    parts = _relative_parts(reference.removeprefix(_PREFIX))
    candidate = root.joinpath(*parts).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("runtime reference escapes configured root") from exc
    return candidate


def normalize_runtime_references(database_path: Path, runtime_root: Path) -> int:
    """Convert legacy absolute runtime paths to confined logical references."""
    root = Path(runtime_root).resolve(strict=True)
    connection = connect(database_path)
    updated = 0
    try:
        with transaction(connection):
            for table, column in _REFERENCE_COLUMNS:
                rows = connection.execute(
                    f"SELECT rowid,{column} AS reference FROM {table} WHERE {column} IS NOT NULL"
                ).fetchall()
                for row in rows:
                    current = str(row["reference"])
                    if current.startswith(_PREFIX):
                        _relative_parts(current.removeprefix(_PREFIX))
                        continue
                    candidate = Path(current)
                    if candidate.is_absolute():
                        resolved = candidate.resolve(strict=False)
                        try:
                            relative = resolved.relative_to(root)
                        except ValueError as exc:
                            raise ValueError("persisted runtime reference escapes configured root") from exc
                    else:
                        relative = Path(*_relative_parts(current))
                    logical = _PREFIX + PurePosixPath(*relative.parts).as_posix()
                    connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE rowid=?",
                        (logical, row["rowid"]),
                    )
                    updated += 1
    finally:
        connection.close()
    return updated
