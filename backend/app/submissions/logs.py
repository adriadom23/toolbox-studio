from __future__ import annotations

import base64
import binascii
import queue
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.submissions.database import connect, transaction
from app.submissions.providers import AppendResult, LogPage
from app.submissions.repository_v2 import now_iso

_SECRET_KEY_NAMES = (
    "token|password|secret|api[_-]?key|aws_access_key_id|aws_secret_access_key|"
    "aws_session_token|azure_client_secret|google_application_credentials"
)
_SECRET_PATTERNS = (
    re.compile(
        rf"(?i)([\"']?(?:{_SECRET_KEY_NAMES})[\"']?\s*[:=]\s*)"
        r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
    ),
    re.compile(
        r"(?i)([\"']?authorization[\"']?\s*[:=]\s*(?:[\"']?Bearer\s+)?)"
        r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
    ),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
_REDACTION_OVERLAP_BYTES = 512


def _replacement(match: re.Match[str]) -> str:
    if not match.lastindex:
        return "[REDACTED]"
    value = match.group(2)
    quote = value[:1] if value[:1] in {"\"", "'"} else ""
    return match.group(1) + quote + "[REDACTED]" + quote


def redact_text(value: str) -> str:
    text = str(value).replace("\x00", "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_replacement, text)
    return text


class LocalLogStore:
    def __init__(self, root: Path, max_phase_bytes: int = 5 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.max_phase_bytes = max_phase_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str, phase: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", ref) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", phase):
            raise ValueError("invalid log reference")
        return self.root / ref / f"{phase}.log"

    def append(self, ref: str, phase: str, safe_chunk: str) -> AppendResult:
        path = self._path(ref, phase)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.stat().st_size if path.exists() else 0
        overlap_size = min(current, _REDACTION_OVERLAP_BYTES)
        prefix_size = current - overlap_size
        previous_tail = b""
        if overlap_size:
            with path.open("rb") as handle:
                handle.seek(prefix_size)
                previous_tail = handle.read(overlap_size)
        combined = previous_tail.decode("utf-8", "replace") + str(safe_chunk)
        encoded = redact_text(combined).encode("utf-8", "replace")
        remaining = max(0, self.max_phase_bytes - prefix_size)
        written = encoded[:remaining]
        mode = "r+b" if path.exists() else "xb"
        with path.open(mode) as handle:
            handle.seek(prefix_size)
            handle.write(written)
            handle.truncate()
            handle.flush()
        appended = max(0, len(written) - len(previous_tail))
        return AppendResult(appended, len(encoded) > remaining)

    def read_page(self, ref: str, phase: str, cursor: str | None = None, line_limit: int = 200, byte_limit: int = 64 * 1024) -> LogPage:
        path = self._path(ref, phase)
        if not path.is_file() or path.is_symlink():
            return LogPage((), None)
        offset = 0
        if cursor:
            try:
                offset = int(base64.urlsafe_b64decode(cursor + "===").decode("ascii"))
            except (binascii.Error, ValueError, UnicodeDecodeError):
                raise ValueError("invalid log cursor") from None
        size = path.stat().st_size
        if offset < 0 or offset > size:
            raise ValueError("invalid log cursor")
        lines: list[str] = []
        consumed = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            while len(lines) < min(max(line_limit, 1), 1000):
                raw = handle.readline()
                if not raw:
                    break
                if consumed + len(raw) > min(max(byte_limit, 1), 1024 * 1024):
                    handle.seek(-len(raw), 1)
                    break
                consumed += len(raw)
                lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
            end = handle.tell()
            more = bool(handle.read(1))
        next_cursor = base64.urlsafe_b64encode(str(end).encode("ascii")).decode("ascii").rstrip("=") if more else None
        return LogPage(tuple(lines), next_cursor, has_more=more)


class LogExportDispatcher:
    _inflight_lock = threading.Lock()
    _inflight: dict[tuple[str, str, str], threading.Thread] = {}

    def __init__(self, database_path: Path, spool: LocalLogStore, exporter: object, *, timeout_seconds: float = 5.0) -> None:
        self.database_path = Path(database_path)
        self.spool = spool
        self.exporter = exporter
        self.timeout_seconds = max(float(timeout_seconds), 0.01)

    def _key(self, submission_id: str, phase: str) -> tuple[str, str, str]:
        return str(self.database_path.resolve()), submission_id, phase

    def _claim(self):
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    "SELECT * FROM submission_log_exports WHERE status='pending' ORDER BY updated_at LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                owner = "log-export-" + uuid.uuid4().hex
                now = datetime.now(timezone.utc)
                lease_until = (now + timedelta(seconds=max(int(self.timeout_seconds) + 5, 10))).isoformat()
                cursor = connection.execute(
                    "UPDATE submission_log_exports SET status='exporting',attempt=attempt+1,lease_owner=?,lease_until=?,updated_at=? "
                    "WHERE submission_id=? AND phase=? AND status='pending' AND attempt=?",
                    (owner, lease_until, now_iso(), row["submission_id"], row["phase"], row["attempt"]),
                )
                if cursor.rowcount != 1:
                    return None
                return str(row["submission_id"]), str(row["phase"]), owner
        finally:
            connection.close()

    def _read_spool(self, submission_id: str, phase: str) -> str:
        cursor = None
        lines: list[str] = []
        while True:
            page = self.spool.read_page(submission_id, phase, cursor, 500, 1024 * 1024)
            lines.extend(page.lines)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        return "\n".join(lines)

    def _settle_late_result(self, submission_id: str, phase: str, owner: str, result: BaseException | None) -> None:
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                if result is None:
                    connection.execute(
                        "UPDATE submission_log_exports SET status='exported',lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=? "
                        "WHERE submission_id=? AND phase=? AND lease_owner=? AND status='exporting'",
                        (now_iso(), submission_id, phase, owner),
                    )
                else:
                    connection.execute(
                        "UPDATE submission_log_exports SET lease_owner=NULL,lease_until=NULL,last_error_code='log_export_delivery_unknown',updated_at=? "
                        "WHERE submission_id=? AND phase=? AND lease_owner=? AND status='exporting'",
                        (now_iso(), submission_id, phase, owner),
                    )
        finally:
            connection.close()

    def _export_bounded(self, submission_id: str, phase: str, content: str, owner: str | None = None) -> None:
        result_queue: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)
        decision = threading.Event()
        timed_out = threading.Event()
        key = self._key(submission_id, phase)

        def invoke() -> None:
            result: BaseException | None = None
            try:
                self.exporter.append(submission_id, phase, content)
            except BaseException as exc:
                result = exc
            result_queue.put(result)
            decision.wait()
            try:
                if timed_out.is_set() and owner is not None:
                    self._settle_late_result(submission_id, phase, owner, result)
            finally:
                with self._inflight_lock:
                    self._inflight.pop(key, None)

        with self._inflight_lock:
            existing = self._inflight.get(key)
            if existing is not None and existing.is_alive():
                raise RuntimeError("log export is already in flight")
            thread = threading.Thread(target=invoke, name=f"log-export-{submission_id[-8:]}", daemon=True)
            self._inflight[key] = thread
        thread.start()
        try:
            result = result_queue.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            timed_out.set()
            decision.set()
            raise TimeoutError("log export timed out") from exc
        decision.set()
        thread.join(timeout=0.1)
        if result is not None:
            raise result

    def retry_ambiguous(self, submission_id: str, phase: str) -> bool:
        key = self._key(submission_id, phase)
        with self._inflight_lock:
            active = self._inflight.get(key)
            if active is not None and active.is_alive():
                return False
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                cursor = connection.execute(
                    "UPDATE submission_log_exports SET status='pending',lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=? "
                    "WHERE submission_id=? AND phase=? AND status='exporting' AND last_error_code IN ('log_export_timeout','log_export_delivery_unknown')",
                    (now_iso(), submission_id, phase),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def dispatch_pending(self, limit: int = 10) -> int:
        completed = 0
        for _ in range(max(0, min(int(limit), 100))):
            claimed = self._claim()
            if claimed is None:
                break
            submission_id, phase, owner = claimed
            error_code = None
            try:
                self._export_bounded(
                    submission_id,
                    phase,
                    self._read_spool(submission_id, phase),
                    owner,
                )
            except Exception as exc:
                error_code = "log_export_timeout" if isinstance(exc, TimeoutError) else "log_export_failed"
            connection = connect(self.database_path)
            try:
                with transaction(connection):
                    if error_code is None:
                        cursor = connection.execute(
                            "UPDATE submission_log_exports SET status='exported',lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=? "
                            "WHERE submission_id=? AND phase=? AND lease_owner=? AND status='exporting'",
                            (now_iso(), submission_id, phase, owner),
                        )
                        completed += int(cursor.rowcount == 1)
                    elif error_code == "log_export_timeout":
                        connection.execute(
                            "UPDATE submission_log_exports SET last_error_code=?,updated_at=? "
                            "WHERE submission_id=? AND phase=? AND lease_owner=? AND status='exporting'",
                            (error_code, now_iso(), submission_id, phase, owner),
                        )
                    else:
                        connection.execute(
                            "UPDATE submission_log_exports SET status='pending',lease_owner=NULL,lease_until=NULL,last_error_code=?,updated_at=? "
                            "WHERE submission_id=? AND phase=? AND lease_owner=? AND status='exporting'",
                            (error_code, now_iso(), submission_id, phase, owner),
                        )
            finally:
                connection.close()
        return completed
