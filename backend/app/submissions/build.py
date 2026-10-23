from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.submissions.logs import redact_text
from app.submissions.providers import BuildHandle, BuildOutputs, BuildPoll, BuildRequest, PreflightResult

_ALLOWED_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "CI", "NO_COLOR"}


class BuildFailed(RuntimeError):
    def __init__(self, code: str, detail: str = "", *, phases: tuple[dict[str, object], ...] = ()) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.phases = phases


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def sanitized_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key.upper() in _ALLOWED_ENV}
    environment.update(extra or {})
    return environment


def _windows_job(process: subprocess.Popen[bytes], *, memory_bytes: int, max_processes: int):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = EXTENDED_LIMIT()
    limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000100 | 0x00000008
    limits.BasicLimitInformation.ActiveProcessLimit = max_processes
    limits.ProcessMemoryLimit = memory_bytes
    if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
        kernel32.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    return handle


def _close_windows_handle(handle) -> None:
    if handle and os.name == "nt":
        import ctypes
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _posix_limits(memory_bytes: int, max_processes: int):
    if os.name == "nt":
        return None

    def apply() -> None:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))

    return apply


def _run(
    argv: list[str], *, cwd: Path, timeout: int, environment: dict[str, str], max_output: int,
    input_data: bytes | None = None, memory_bytes: int = 2 * 1024 * 1024 * 1024, max_processes: int = 32,
    cancellation_event: object | None = None, pass_fds: tuple[int, ...] = (),
) -> CommandResult:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    popen_options = {"pass_fds": pass_fds} if os.name != "nt" else {}
    process = subprocess.Popen(
        argv, cwd=cwd, env=environment, shell=False,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
        creationflags=creationflags, start_new_session=os.name != "nt",
        preexec_fn=_posix_limits(memory_bytes, max_processes),
        **popen_options,
    )
    job = None
    try:
        job = _windows_job(process, memory_bytes=memory_bytes, max_processes=max_processes)
    except OSError:
        _terminate_tree(process)
        raise BuildFailed("build_limits_unavailable") from None

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def drain(name: str, stream) -> None:
        while chunk := stream.read(65536):
            remaining = max_output - len(buffers[name])
            if remaining > 0:
                buffers[name].extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[name] = True

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if input_data is not None and process.stdin is not None:
        try:
            process.stdin.write(input_data)
            process.stdin.close()
        except BrokenPipeError:
            pass
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if cancellation_event is not None and bool(getattr(cancellation_event, "is_set")()):
                cancelled = True
                _terminate_tree(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_tree(process)
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        process.wait()
    finally:
        for reader in readers:
            reader.join(timeout=5)
        _close_windows_handle(job)
    safe_out = redact_text(bytes(buffers["stdout"]).decode("utf-8", "replace"))
    safe_err = redact_text(bytes(buffers["stderr"]).decode("utf-8", "replace"))
    result = CommandResult(process.returncode, safe_out, safe_err, truncated["stdout"], truncated["stderr"])
    if cancelled:
        raise BuildFailed("build_cancelled", safe_err or safe_out, phases=({"phase": "process", "returncode": -1, "stdout": safe_out, "stderr": safe_err, "truncated": any(truncated.values())},))
    if timed_out:
        raise BuildFailed("build_timeout", safe_err or safe_out, phases=({"phase": "process", "returncode": -1, "stdout": safe_out, "stderr": safe_err, "truncated": any(truncated.values())},))
    return result


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)


class LocalSubprocessBuildProvider:
    provider = "local-subprocess"
    isolated = False

    def __init__(
        self,
        frontend_root: Path,
        runner_root: Path | None = None,
        *,
        python_executable: Path | None = None,
        max_log_bytes: int = 5 * 1024 * 1024,
        max_builds_global: int = 2,
        max_builds_per_project: int = 1,
        capacity_wait_seconds: float = 5.0,
    ) -> None:
        self.frontend_root = Path(frontend_root)
        self.runner_root = Path(runner_root or Path(__file__).with_name("runners"))
        self.python_executable = Path(python_executable or sys.executable)
        self.max_log_bytes = max_log_bytes
        self.max_builds_global = max(int(max_builds_global), 1)
        self.max_builds_per_project = max(int(max_builds_per_project), 1)
        self.capacity_wait_seconds = max(float(capacity_wait_seconds), 0.01)
        self._global_slots = threading.BoundedSemaphore(self.max_builds_global)
        self._project_slot_lock = threading.Lock()
        self._project_slots: dict[str, threading.BoundedSemaphore] = {}
        self._outputs: dict[str, BuildOutputs] = {}

    @contextmanager
    def _build_slot(self, project_slug: str):
        with self._project_slot_lock:
            project_slot = self._project_slots.setdefault(
                project_slug,
                threading.BoundedSemaphore(self.max_builds_per_project),
            )
        if not self._global_slots.acquire(timeout=self.capacity_wait_seconds):
            raise BuildFailed("build_capacity_exceeded")
        try:
            if not project_slot.acquire(timeout=self.capacity_wait_seconds):
                raise BuildFailed("project_build_capacity_exceeded")
            try:
                yield
            finally:
                project_slot.release()
        finally:
            self._global_slots.release()

    def preflight(self) -> PreflightResult:
        vite = self.frontend_root / "node_modules" / "vite" / "bin" / "vite.js"
        return PreflightResult(vite.is_file() and self.python_executable.is_file(), "" if vite.is_file() else "vite is unavailable")

    def _harness_sha(self) -> str:
        digest = hashlib.sha256()
        for path in (self.frontend_root / "package.json", self.frontend_root / "package-lock.json", self.runner_root / "submission-vite.config.mjs", self.runner_root / "validate_submission_project.py"):
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _phase(name: str, result: CommandResult) -> dict[str, object]:
        return {"phase": name, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "truncated": result.stdout_truncated or result.stderr_truncated}

    @staticmethod
    def _timeout_phase(name: str, exc: BuildFailed) -> tuple[dict[str, object], ...]:
        phase = dict(exc.phases[0]) if exc.phases else {"returncode": -1, "stdout": "", "stderr": exc.detail}
        phase["phase"] = name
        return (phase,)

    def execute(self, request: BuildRequest) -> BuildOutputs:
        with self._build_slot(request.project_slug):
            return self._execute_locked(request)

    def _execute_locked(self, request: BuildRequest) -> BuildOutputs:
        preflight = self.preflight()
        if not preflight.available:
            raise BuildFailed("build_provider_unavailable", preflight.detail)
        request.output_root.mkdir(parents=True, exist_ok=True)
        component_name = "".join(part[:1].upper() + part[1:] for part in request.project_slug.split("-")) + ".vue"
        component = request.snapshot_root / "frontend" / "src" / "projects" / request.project_slug / component_name
        if not component.is_file():
            raise BuildFailed("frontend_entry_missing")
        harness_input = request.output_root / "harness"
        harness_input.mkdir(parents=True, exist_ok=True)
        (harness_input / "index.html").write_text('<div id="app"></div><script type="module" src="/submission-entry.js"></script>\n', encoding="utf-8")
        (harness_input / "submission-entry.js").write_text('import { createApp } from "vue";\nimport Component from "@toolbox-component";\ncreateApp(Component).mount("#app");\n', encoding="utf-8")
        environment = sanitized_environment({
            "TOOLBOX_FRONTEND_ROOT": str(self.frontend_root), "TOOLBOX_SNAPSHOT_ROOT": str(request.snapshot_root),
            "TOOLBOX_BUILD_INPUT": str(harness_input), "TOOLBOX_COMPONENT_PATH": str(component),
        })
        node = shutil_which("node", environment.get("PATH", ""))
        if not node:
            raise BuildFailed("node_unavailable")
        deadline = time.monotonic() + request.timeout_seconds

        def remaining() -> int:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise BuildFailed("build_timeout")
            return max(1, int(seconds))

        vite_argv = [node, str(self.frontend_root / "node_modules" / "vite" / "bin" / "vite.js"), "build", "--config", str(self.runner_root / "submission-vite.config.mjs"), "--outDir", str(request.output_root / "preview")]
        try:
            vite = _run(vite_argv, cwd=harness_input, timeout=remaining(), environment=environment, max_output=self.max_log_bytes, cancellation_event=request.cancellation_event)
        except BuildFailed as exc:
            raise BuildFailed(exc.code, exc.detail, phases=self._timeout_phase("frontend", exc)) from None
        phases: list[dict[str, object]] = [self._phase("frontend", vite)]
        if vite.returncode:
            raise BuildFailed("vite_build_failed", vite.stderr, phases=tuple(phases))
        backend_argv = [str(self.python_executable), "-I", "-B", str(self.runner_root / "validate_submission_project.py"), str(request.snapshot_root), request.project_slug]
        try:
            backend = _run(backend_argv, cwd=request.output_root, timeout=remaining(), environment=environment, max_output=self.max_log_bytes, cancellation_event=request.cancellation_event)
        except BuildFailed as exc:
            raise BuildFailed(exc.code, exc.detail, phases=tuple(phases) + self._timeout_phase("backend", exc)) from None
        phases.append(self._phase("backend", backend))
        if backend.returncode:
            raise BuildFailed("backend_validation_failed", backend.stderr or backend.stdout, phases=tuple(phases))
        try:
            payload = json.loads(backend.stdout.strip())
        except ValueError as exc:
            raise BuildFailed("backend_validation_protocol", phases=tuple(phases)) from exc
        if set(payload) != {"version", "ok", "checks"} or payload["version"] != 1 or payload["ok"] is not True:
            raise BuildFailed("backend_validation_protocol", phases=tuple(phases))
        return BuildOutputs(True, self._harness_sha(), tuple(phases))

    def start(self, request: BuildRequest) -> BuildHandle:
        handle = BuildHandle("local-" + uuid.uuid4().hex, request.marker)
        try:
            self._outputs[handle.id] = self.execute(request)
        except BuildFailed as exc:
            self._outputs[handle.id] = BuildOutputs(False, self._harness_sha(), phases=exc.phases, error_code=exc.code)
        return handle

    def reconcile(self, marker: str, window_seconds: int) -> list[BuildHandle]:
        del marker, window_seconds
        return []

    def poll(self, handle: BuildHandle) -> BuildPoll:
        output = self._outputs.get(handle.id)
        if output is None:
            return BuildPoll("unknown", "build_unknown")
        return BuildPoll("succeeded" if output.ok else "failed", output.error_code)

    def fetch_outputs(self, handle: BuildHandle, expected: dict[str, str] | None = None) -> BuildOutputs:
        del expected
        return self._outputs[handle.id]


def shutil_which(command: str, path: str) -> str | None:
    import shutil
    return shutil.which(command, path=path)
