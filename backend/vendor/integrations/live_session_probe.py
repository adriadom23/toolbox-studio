"""TTL cache for *live* session probes behind the connection status chips.

The passive cookie checks (``firefox_cookies.session_status`` and friends)
only prove the cookies exist on disk and, for JWTs, that ``exp`` has not
passed. A session can be invalidated server-side while still looking valid
locally, which paints the chip "Conectado" while every extraction fails with
an auth error — and, because the status endpoint keeps disagreeing with the
extraction, the UI flips right back to green after each failure.

Status providers fix that by wrapping ONE real HTTP probe (the same request
the extraction depends on) with :func:`cached_probe`, so the chip reports
what the extraction will actually see without hammering the site from the
frontend's 4-second reconnect loop:

* outcomes are cached per ``(source, cookie-fingerprint)`` with per-outcome
  TTLs (a confirmed session is re-checked lazily; a rejected one is re-tried
  sooner);
* a fresh login rotates the cookie values → new fingerprint → cache miss, so
  the reconnect loop detects the new session on its next tick;
* concurrent status calls share a single in-flight probe per source.

Probe outcomes:

* ``"ok"``    — the server accepted the session; extraction should work.
* ``"auth"``  — the server rejected the session (redirect to SSO, 401/403,
  login page…); the operator must re-login even though cookies look fine.
* ``"error"`` — the probe could not tell (network error, 5xx). Callers keep
  the passive verdict in this case so a transient blip doesn't flap the chip.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Callable, Literal

Outcome = Literal["ok", "auth", "error"]

#: How long a confirmed-live session is trusted before re-probing.
OK_TTL = 120.0
#: How long a server-side rejection is remembered (same cookies).
AUTH_TTL = 30.0
#: How long an inconclusive probe (network / 5xx) is remembered.
ERROR_TTL = 15.0

_guard = threading.Lock()
_probe_locks: dict[str, threading.Lock] = {}
_cache: dict[str, tuple[float, Outcome]] = {}


def cookie_fingerprint(*values: str | None) -> str:
    """Stable short digest of the cookie values that authenticate a session."""
    joined = "\x1f".join(v or "" for v in values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def clear_cache() -> None:
    """Drop every cached outcome (tests)."""
    with _guard:
        _cache.clear()


def cached_probe(
    source: str,
    fingerprint: str,
    probe: Callable[[], Outcome],
    *,
    ok_ttl: float = OK_TTL,
    auth_ttl: float = AUTH_TTL,
    error_ttl: float = ERROR_TTL,
) -> Outcome:
    """Return the (possibly cached) live outcome of ``probe`` for ``source``.

    ``fingerprint`` must change whenever the session cookies change (use
    :func:`cookie_fingerprint`), so a re-login bypasses any cached rejection
    immediately. ``probe`` runs at most once at a time per ``source``; any
    exception it raises is treated as ``"error"``.
    """
    key = f"{source}:{fingerprint}"
    with _guard:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        lock = _probe_locks.setdefault(source, threading.Lock())

    with lock:
        # Another caller may have probed while we waited for the lock.
        with _guard:
            hit = _cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        try:
            outcome: Outcome = probe()
        except Exception:  # noqa: BLE001 - a broken probe must never 500 the panel
            outcome = "error"
        if outcome not in ("ok", "auth", "error"):
            outcome = "error"
        ttl = {"ok": ok_ttl, "auth": auth_ttl, "error": error_ttl}[outcome]
        now = time.monotonic()
        with _guard:
            # Evict this source's stale entries (old fingerprints / expired).
            prefix = f"{source}:"
            for k in [
                k
                for k, (expires, _o) in _cache.items()
                if k.startswith(prefix) and (k != key or expires <= now)
            ]:
                _cache.pop(k, None)
            _cache[key] = (now + ttl, outcome)
        return outcome


def record_outcome(
    source: str,
    fingerprint: str,
    outcome: Outcome,
    *,
    ttl: float | None = None,
) -> None:
    """Seed the cache with an outcome observed OUTSIDE a probe.

    Extraction clients call this when a real request is rejected mid-session
    (e.g. a 401 from the data endpoint), so the next status check reports the
    rejection immediately instead of trusting a still-fresh "ok" entry.
    """
    if outcome not in ("ok", "auth", "error"):
        return
    if ttl is None:
        ttl = {"ok": OK_TTL, "auth": AUTH_TTL, "error": ERROR_TTL}[outcome]
    with _guard:
        _cache[f"{source}:{fingerprint}"] = (time.monotonic() + ttl, outcome)


__all__ = ["Outcome", "cached_probe", "cookie_fingerprint", "clear_cache", "record_outcome"]
