from __future__ import annotations

from pathlib import Path

# --- Pin the Werkzeug reloader to the backend tree -------------------------
#
# Mirrors ToolBox's own backend/app/main.py: Werkzeug's --debug reloader
# auto-discovers watch paths from sys.path and from every imported
# module's __file__, which on Windows pulls in
# %APPDATA%\Roaming\Python\PythonXX\site-packages and
# C:\Program Files\PythonXXX — AV scans / pip / one-time hook writes there
# then trigger spurious reloads mid-request. We bypass Werkzeug's
# auto-discovery and tell the reloader to observe ONLY backend/app/**/*.py
# (this also covers every scaffolded backend/app/projects/<name>/routes.py,
# so editing a project's backend half live-reloads it too).
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_WATCH_ROOT = str((_BACKEND_DIR / "app").resolve())

try:
    from werkzeug import _reloader as _wz_reloader

    _NOWATCH_PARTS = {"__pycache__", ".git", ".pytest_cache", ".hypothesis", "data"}

    def _watchdog_paths(extra_files, exclude_patterns):  # noqa: ANN001
        return {_WATCH_ROOT}

    def _stat_paths(extra_files, exclude_patterns):  # noqa: ANN001
        paths: set[str] = set()
        for candidate in Path(_WATCH_ROOT).rglob("*.py"):
            if any(part in _NOWATCH_PARTS for part in candidate.parts):
                continue
            paths.add(str(candidate))
        for extra in (extra_files or ()):
            paths.add(str(extra))
        return paths

    _wz_reloader._find_watchdog_paths = _watchdog_paths  # type: ignore[attr-defined]
    _wz_reloader._find_stat_paths = _stat_paths  # type: ignore[attr-defined]

    # --- Reload on newly-created files, not just edited ones ------------
    #
    # Werkzeug's own StatReloaderLoop.run_step only triggers a reload when
    # a path IT ALREADY KNEW ABOUT gets a newer mtime; a path it is seeing
    # for the first time is silently absorbed into its baseline
    # (`self.mtimes[name] = mtime; continue`) without reloading. Studio's
    # "create project" flow (app/projects/scaffold.py) atomically renames
    # a freshly-written `app/projects/<name>/routes.py` into place while
    # this dev server is already running — exactly the case Werkzeug's
    # reloader does not treat as a change, so the new project's blueprint
    # (registered once, at `create_app()` time — see app/projects/
    # registry.py) would stay unreachable (404) until some unrelated edit
    # elsewhere touched an already-watched file. That breaks this
    # project's own promise, both to a Studio developer creating a project
    # while `npm run dev` is running and to README.md's "already wired
    # up, with zero manual registration steps". Wrap run_step so a path
    # appearing for the first time *after* the loop's own first step
    # (i.e. not part of the initial baseline) also triggers a reload.
    _StatReloaderLoop = _wz_reloader.StatReloaderLoop
    _orig_run_step = _StatReloaderLoop.run_step

    def _run_step_detect_new_files(self):  # noqa: ANN001
        seen_before = set(self.mtimes)
        is_first_step = not seen_before
        _orig_run_step(self)
        if is_first_step:
            return  # the loop's own initial baseline is not a "change"
        newly_seen = set(self.mtimes) - seen_before
        if newly_seen:
            self.trigger_reload(sorted(newly_seen)[0])

    _StatReloaderLoop.run_step = _run_step_detect_new_files  # type: ignore[method-assign]
except Exception:  # pragma: no cover - fail-soft if werkzeug API changes
    pass


from app import create_app

app = create_app()


if __name__ == "__main__":
    _NOWATCH_DIRS = {"data", "__pycache__", ".git", ".pytest_cache", ".hypothesis"}
    extra_files = [
        str(p)
        for p in (_BACKEND_DIR / "app").rglob("*.py")
        if not any(part in _NOWATCH_DIRS for part in p.relative_to(_BACKEND_DIR).parts)
    ]

    exclude_patterns = [
        "*/__pycache__/*",
        "*/.git/*",
        "*/.pytest_cache/*",
        "*/.hypothesis/*",
        "*/node_modules/*",
    ]

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
        threaded=True,
        use_reloader=True,
        extra_files=extra_files,
        exclude_patterns=exclude_patterns,
    )
