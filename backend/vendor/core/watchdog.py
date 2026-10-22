"""Soft hooks into the launcher heartbeat watchdog.

The bundled exe (``build/heartbeat.py``) installs a watchdog thread
that exits the process when the SPA stops pinging. Long-running
endpoints (workbook writes against a slow shared drive) need to
suppress the watchdog so the user does not get
``NetworkError when attempting to fetch resource`` mid-save.

This module exposes a thin abstraction:

* ``watchdog_pause()`` is a context manager that pauses the watchdog
  for the duration of a block of code. Inside the bundle it forwards
  to ``build/heartbeat.py``; in dev mode it is a no-op.

The dev backend never installs the watchdog, so the no-op fallback
is fine for ``flask --debug``.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


def _try_import_watchdog():
    """Return the ``build.heartbeat`` module when available, else ``None``."""
    try:
        import heartbeat as _heartbeat  # type: ignore[import-not-found]

        return _heartbeat
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def watchdog_pause() -> Iterator[None]:
    """Pause the launcher watchdog for the duration of the block.

    Safe to call from dev mode (no watchdog installed); the context
    manager just yields.
    """
    module = _try_import_watchdog()
    pause = getattr(module, "pause_watchdog", None) if module else None
    resume = getattr(module, "resume_watchdog", None) if module else None
    if pause is not None:
        try:
            pause()
        except Exception:  # noqa: BLE001
            pass
    try:
        yield
    finally:
        if resume is not None:
            try:
                resume()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["watchdog_pause"]
