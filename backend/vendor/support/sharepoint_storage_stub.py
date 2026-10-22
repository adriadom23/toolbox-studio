"""Local Studio replacement for ToolBox's `app.shared_excel.sharepoint_storage`.

ToolBox's real module (1479 lines) is a SharePoint REST sync layer backed
by `app.integrations.sharepoint_client`, `requests`, `threading`, and
`zipfile`. Studio does not have a station-workbook/SharePoint world, so
this stub implements just the subset of the real module's public API that
the vendored integration clients (`horizon_metrics_store.py`,
`manifest_lookup.py`, `rts_persistence.py`) actually call.

`is_configured()` always returns False here (Studio never wires up
`TOOLBOX_STUDIO_SHAREPOINT_SITE_URL` to a real SharePoint site), which is
behaviorally identical to the real module running on a station that has
not configured SharePoint yet (see design.md §1.3: the real function is
literally `bool(sharepoint_site_url()) and bool(station_code())`).

Every function signature matches the real module so a future "turn
SharePoint on for Studio" upgrade only needs to swap the import target in
`app/shared_excel/sharepoint_storage.py`, not touch any call site.
"""

import os
from pathlib import Path
from typing import Iterable, Optional


def _studio_data_dir() -> Path:
    # vendor/support/sharepoint_storage_stub.py -> vendor/support ->
    # vendor -> backend, then into data/cache/.
    data_dir = Path(__file__).resolve().parents[2] / "data" / "cache"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def is_configured() -> bool:
    """Return True only if Studio's own SharePoint env var is set.

    Studio ships with no SharePoint integration configured by default, so
    this returns False unless an operator has explicitly set
    `TOOLBOX_STUDIO_SHAREPOINT_SITE_URL` in the environment.
    """

    return bool(os.environ.get("TOOLBOX_STUDIO_SHAREPOINT_SITE_URL", "").strip())


def station_cache_dir(*parts: str) -> Path:
    """Return a local directory under `backend/data/cache/<parts>`.

    Always succeeds and never touches the network, even though real
    callers may invoke this without gating on `is_configured()` first
    (confirmed call sites in `horizon_metrics_store.py` — see
    design.md §1.2).
    """

    target = _studio_data_dir()
    for part in parts:
        target = target / str(part)
    target.mkdir(parents=True, exist_ok=True)
    return target


def pull_cache_path(path: str) -> Optional[Path]:
    """No-op passthrough: there is nothing to pull from, so this never
    resolves to a real cached file."""

    return None


def push_file(path) -> None:
    """No-op: Studio has nothing configured to push to."""

    return None


def pull_folder(parts: Iterable[str], suffixes=None) -> list:
    """Always returns an empty list, letting callers fall through to
    their own local glob fallback (confirmed behavior for the ungated
    call site in `horizon_metrics_store.py` — see design.md §1.2)."""

    return []


def download_manifest(name: str):
    """No manifest to download in Studio's local-only world."""

    return None


def upload_manifest(name: str, content) -> None:
    """No-op: nothing to upload to."""

    return None


def prune_old_manifests() -> None:
    """No-op: no remote manifest store to prune."""

    return None


def list_manifests() -> list:
    """Always returns an empty list: no remote manifest store."""

    return []
