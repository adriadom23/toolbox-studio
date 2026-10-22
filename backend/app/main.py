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
