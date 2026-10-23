from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.errors import PublicError
from app.submissions.database import connect

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(database_path: Path, slug: str) -> threading.RLock:
    key = str(Path(database_path).resolve()) + "\0" + slug
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def workspace_lock(database_path: Path, slug: str) -> Iterator[None]:
    """Serialize one project's saves, reads, runtimes, and snapshots."""
    lock_path = Path(database_path).parent / "workspace-locks" / f"{slug}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock(database_path, slug)
    with thread_lock, lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_workspace_ready(database_path: Path, slug: str) -> None:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT status,error_code FROM workspace_save_recovery "
            "WHERE project_slug=? AND status!='completed' ORDER BY updated_at DESC LIMIT 1",
            (slug,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise PublicError(
            "workspace_recovering",
            "workspace recovery state is unavailable",
            503,
            recoverable=True,
        ) from exc
    finally:
        connection.close()
    if row is not None:
        raise PublicError(
            "workspace_recovering",
            "workspace update is recovering; retry after readiness returns healthy",
            503,
            recoverable=True,
        )
