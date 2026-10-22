"""Per-request selected-station context.

The frontend navbar lets the operator pick a station ("DCZ4"), a
secondary one ("DCZ3"), or "Todos" (the ``__ALL__`` sentinel). That
choice is shipped on every tool run as ``payload["site"]`` (see
``frontend/src/shared/api/http.js``).

Historically each tool resolved its station(s) straight from the
``STATION_CODE`` / ``STATION_CODES`` environment variables and a
multisite tool looped over *all* configured stations. This module makes
the navbar selection available to every script through a
``contextvars.ContextVar`` set once in ``run_tool`` and consulted by the
shared ``resolve_station_code`` / ``resolve_station_codes`` helpers.

Design rules:
  * A *specific* selection (e.g. "DCZ4") scopes the tool to that station.
  * The ``__ALL__`` sentinel (or no selection) keeps the legacy behaviour
    of acting on every configured station.
  * A stale/unknown selection never narrows to an empty set: helpers fall
    back to the configured codes so a tool can't silently do nothing.
"""

from __future__ import annotations

import contextvars
from typing import Iterable

# Sentinel emitted by the navbar "Todos" option.
ALL_SITES = "__ALL__"

_selected_site: contextvars.ContextVar[str] = contextvars.ContextVar(
    "toolbox_selected_site", default=""
)


def set_selected_site(value: str | None) -> contextvars.Token:
    """Store the navbar selection for the current execution.

    Returns the token so the caller can restore the previous value with
    :func:`reset_selected_site` once the tool finishes.
    """
    site = (value or "").strip().upper()
    return _selected_site.set(site)


def reset_selected_site(token: contextvars.Token | None) -> None:
    if token is None:
        return
    try:
        _selected_site.reset(token)
    except (ValueError, LookupError, RuntimeError):
        # Token from a different context (e.g. crossed an executor
        # boundary) or already consumed once (teardown fired twice for
        # the same request, as the test client does). Best-effort reset:
        # clear instead of raising.
        _selected_site.set("")


def get_selected_site() -> str:
    """Return the upper-cased navbar selection, or "" when none/all."""
    return _selected_site.get()


def is_specific_site() -> bool:
    """True when the operator picked a single concrete station."""
    site = get_selected_site()
    return bool(site) and site != ALL_SITES


def resolve_station_codes(all_codes: Iterable[str]) -> list[str]:
    """Scope a list of configured station codes by the navbar selection.

    * Specific selection present in ``all_codes`` -> ``[selection]``.
    * Otherwise (``__ALL__``, empty, or unknown selection) -> every
      configured code, de-duplicated and upper-cased.
    """
    codes: list[str] = []
    for raw in all_codes or ():
        code = (raw or "").strip().upper()
        if code and code not in codes:
            codes.append(code)
    site = get_selected_site()
    if site and site != ALL_SITES and site in codes:
        return [site]
    return codes


def resolve_station_code(default: str = "") -> str:
    """Return the single station the tool should act on.

    Prefers the concrete navbar selection; falls back to ``default``
    (typically the configured primary ``STATION_CODE``) when "Todos" is
    selected or nothing was sent.
    """
    site = get_selected_site()
    if site and site != ALL_SITES:
        return site
    return (default or "").strip().upper()
