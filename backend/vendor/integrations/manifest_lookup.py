"""Central Plan Manifest helpers.

The Plan Manifest is the same file shared between several tools (FTDS,
Multi-Items, DSC, Lockers, ...). It lives in
``BASE_AM_TB/<year>/<MM. month>/`` with a date-stamped name. This
module is the single source of truth for:

* ``find_daily_manifest(when)`` — locates the file on disk.
* ``persist_uploaded_manifest(path, when)`` — copies an upload into
  ``BASE_AM_TB`` so subsequent tools find it without re-uploading.
* ``resolve_manifest_path(payload, *, when, persist)`` — the recipe
  used by every tool ``run()``: prefer the uploaded file, fall back to
  the auto-detected one, and persist the upload when missing on disk.
* ``load_manifest_context(path)`` — ``tid -> {route, schedule}``
  dictionary built from a PM manifest workbook (legacy FTDS layout).
* ``collect_manifest_tracking_ids(path)`` — flat list of tracking IDs
  used by Multi-Items.

All paths returned are absolute :class:`Path` objects. The functions
silently return ``None`` (or empty results) when ``BASE_AM_TB`` is not
configured or the file does not exist; callers decide how to surface
those situations.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_MONTHS_ES: tuple[str, ...] = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)

# Allowed extensions for the Plan Manifest. Order matters: the auto
# detector picks the first existing file in this exact priority.
MANIFEST_EXTENSIONS: tuple[str, ...] = (".xlsx", ".xlsm", ".xls")

# New storage layout (requested): every Plan Manifest lives under
# ``BASE_TOOLBOX/<YYYY>/Manifest/`` (a year subfolder, like the daily
# workbook) and is named ``<STATION> - <CYCLE> - DD.MM.YYYY.xlsx`` (e.g.
# ``23. Toolbox/2026/Manifest/DCZ4 - CYCLE_1 - 08.06.2026.xlsx``). When no
# cycle is known we drop the cycle segment (``DCZ4 - 08.06.2026.xlsx``).
MANIFEST_FOLDER_NAME = "Manifest"

# Canonical persisted name templates. ``CANONICAL_TEMPLATE`` is kept for
# backwards-compat imports; the active naming uses the helpers below.
CANONICAL_TEMPLATE = "{day:02d}.{month:02d}.{year}.xlsx"
CANONICAL_TEMPLATE_WITH_STATION = "{station} - {day:02d}.{month:02d}.{year}.xlsx"
CANONICAL_TEMPLATE_FULL = "{station} - {cycle} - {day:02d}.{month:02d}.{year}.xlsx"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _to_date(when: date | datetime | None) -> date:
    if when is None:
        return date.today()
    if isinstance(when, datetime):
        return when.date()
    return when


def _clean_env_path(raw: object) -> str:
    if not isinstance(raw, str):
        raw = str(raw or "")
    value = raw.strip().strip('"').strip("'")
    return re.sub(r"\\\\+", r"\\", value)


def _norm_station(code: str | None) -> str:
    text = (code or "").strip().upper()
    if not text or not text.isalnum() or len(text) > 6:
        return ""
    return text


def _norm_cycle(cycle: str | None) -> str:
    text = (cycle or "").strip().upper().replace(" ", "_")
    # Defensive: only ASCII alnum + underscore, max 16 chars.
    cleaned = re.sub(r"[^A-Z0-9_]", "", text)
    return cleaned[:16]


def base_am_dir() -> str:
    return _clean_env_path(os.environ.get("BASE_AM_TB", ""))


def manifest_base_dir() -> str:
    """Return the base directory for the Plan Manifest store.

    The manifest now lives under the unified Toolbox tree
    (``Z:/23. Toolbox`` by default, or ``BASE_TOOLBOX``), NOT under
    ``BASE_AM_TB``. We resolve it through the same helper the daily
    workbook uses so both share the exact same root.
    """
    from app.shared_excel.toolbox_output import get_base_toolbox_dir
    return _clean_env_path(get_base_toolbox_dir())


def month_folder(base: str | Path, when: date | datetime | None = None) -> Path:
    """Return ``<base>/<year>/<MM. month>/`` path object (legacy layout)."""
    target = _to_date(when)
    base_path = Path(_clean_env_path(str(base)))
    return base_path / str(target.year) / f"{target.month:02d}. {_MONTHS_ES[target.month - 1]}"


def manifest_folder(when: date | datetime | None = None) -> Path:
    """Return ``<BASE_TOOLBOX>/<YYYY>/Manifest/`` — the active store.

    Every Plan Manifest lives under the year folder for ``when`` (e.g.
    ``23. Toolbox/2026/Manifest/``), mirroring where the daily workbook
    is stored. The day is still encoded in the filename, so saving and
    lookup for a given date always hit the same path.
    """
    target = _to_date(when)
    return Path(manifest_base_dir()) / str(target.year) / MANIFEST_FOLDER_NAME


def _format_full(station: str, cycle: str, when: date, ext: str) -> str:
    return f"{station} - {cycle} - {when.day:02d}.{when.month:02d}.{when.year}{ext}"


def _format_with_station(station: str, when: date, ext: str) -> str:
    return f"{station} - {when.day:02d}.{when.month:02d}.{when.year}{ext}"


def _format_date_only(when: date, ext: str) -> str:
    return f"{when.day:02d}.{when.month:02d}.{when.year}{ext}"


# --- Legacy filename helpers (read-only fallback for old saved files) ------


def _legacy_format_full(station: str, cycle: str, when: date, ext: str) -> str:
    return (
        f"PlanManifest_{station}_{cycle}_{when.day:02d}."
        f"{when.month:02d}.{when.year}{ext}"
    )


def _legacy_format_with_station(station: str, when: date, ext: str) -> str:
    return f"PlanManifest_{station}_{when.day:02d}.{when.month:02d}.{when.year}{ext}"


def _legacy_format_date_only(when: date, ext: str) -> str:
    return f"PlanManifest_{when.day:02d}.{when.month:02d}.{when.year}{ext}"


def _candidate_filenames(
    when: date,
    *,
    station: str = "",
    cycle: str = "",
) -> tuple[str, ...]:
    """New-layout filenames the auto-detector tries in priority order.

    Priority (highest first):
      1. ``<STATION> - <CYCLE> - DD.MM.YYYY.xlsx`` (full).
      2. ``<STATION> - DD.MM.YYYY.xlsx`` (no cycle).
      3. ``DD.MM.YYYY.xlsx`` (date-only fallback).
    """
    out: list[str] = []
    if station and cycle:
        for ext in MANIFEST_EXTENSIONS:
            out.append(_format_full(station, cycle, when, ext))
    if station:
        for ext in MANIFEST_EXTENSIONS:
            out.append(_format_with_station(station, when, ext))
    for ext in MANIFEST_EXTENSIONS:
        out.append(_format_date_only(when, ext))
    # Dedup while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for name in out:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return tuple(unique)


def _legacy_candidate_filenames(
    when: date,
    *,
    station: str = "",
    cycle: str = "",
) -> tuple[str, ...]:
    """Old-layout filenames kept for read-only back-compat."""
    out: list[str] = []
    if station and cycle:
        for ext in MANIFEST_EXTENSIONS:
            out.append(_legacy_format_full(station, cycle, when, ext))
    if station:
        for ext in MANIFEST_EXTENSIONS:
            out.append(_legacy_format_with_station(station, when, ext))
    for ext in MANIFEST_EXTENSIONS:
        out.append(_legacy_format_date_only(when, ext))
    out.append(f"PM {when.day:02d}.{when.month:02d}.{when.year}.xlsx")
    out.append(f"PM {when.day:02d}.{when.month:02d}.{when.year}.xlsm")
    out.append(f"PM {when.day:02d}.{when.month:02d}.{when.year}.xls")
    seen: set[str] = set()
    unique: list[str] = []
    for name in out:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return tuple(unique)


def find_daily_manifest(
    when: date | datetime | None = None,
    *,
    base_am_dir: str | None = None,  # noqa: A002 — keep public name
    station_code: str | None = None,
    cycle: str | None = None,
) -> Path | None:
    """Return the absolute path to the Plan Manifest for ``when`` or ``None``.

    Search order:
      1. New layout ``<base>/<YYYY>/Manifest/`` with the most specific
         name first (``STATION - CYCLE - date`` → ``STATION - date`` →
         ``date``).
      2. Legacy layout ``<base>/<year>/<MM. month>/`` with old
         ``PlanManifest_*`` / ``PM *`` names (read-only back-compat).
      3. When no ``station_code`` is given (most tools call it like
         this), fall back to *any* manifest found for that date.
    """
    target_date = _to_date(when)
    station = _norm_station(station_code)
    cycle_norm = _norm_cycle(cycle)

    # Single source of truth: ``<BASE_TOOLBOX>/<YYYY>/Manifest/``.
    new_folder = manifest_folder(target_date)
    if station and new_folder.exists():
        for name in _candidate_filenames(target_date, station=station, cycle=cycle_norm):
            candidate = new_folder / name
            if candidate.exists():
                return candidate

    # Best-effort: try SharePoint when the file is missing locally. The
    # cache lives per-user, so a manifest persisted from another machine
    # (or a fresh install with no local files) only exists on SharePoint
    # until another tool downloads it. We try the most specific filename
    # first and fall back to the date-only one.
    if station:
        downloaded = _try_download_manifest_from_sharepoint(
            target_date, station=station, cycle=cycle_norm
        )
        if downloaded is not None and downloaded.exists():
            return downloaded

    # Fallback: any manifest persisted for that date (covers callers
    # that don't pass a station, and date-only filenames). Entries can
    # come from the SharePoint listing with a local ``path`` that does
    # NOT exist yet (fresh install / uploaded from another machine), so
    # each candidate is materialized (downloaded by its real filename)
    # before being returned — otherwise tools fail the ``exists()``
    # check with "manifest not found" while the panel shows it green.
    listed = list_daily_manifests(target_date)
    if listed:
        if station:
            # Prefer the requested station (and cycle when given).
            if cycle_norm:
                for entry in listed:
                    if (
                        str(entry.get("station") or "").upper() == station
                        and str(entry.get("cycle") or "").upper() == cycle_norm
                    ):
                        materialized = _materialize_listed_entry(entry, target_date)
                        if materialized is not None:
                            return materialized
            for entry in listed:
                if str(entry.get("station") or "").upper() == station:
                    materialized = _materialize_listed_entry(entry, target_date)
                    if materialized is not None:
                        return materialized
        for entry in listed:
            materialized = _materialize_listed_entry(entry, target_date)
            if materialized is not None:
                return materialized
    return None


def _materialize_listed_entry(
    entry: dict[str, Any],
    target_date: date,
) -> Path | None:
    """Return a local, existing Path for a ``list_daily_manifests`` entry.

    Local entries are returned as-is. SharePoint-only entries (their
    ``path`` points at the cache location the file WOULD occupy) are
    downloaded by their exact filename — this also covers cycles the
    caller didn't know about (e.g. ``DCZ4 - CYCLE_1 - 19.08.2026.xlsx``
    when the lookup only knew the station). Returns ``None`` when the
    file can't be materialized.
    """
    path = Path(str(entry.get("path") or ""))
    if path.exists():
        return path
    name = str(entry.get("name") or "").strip()
    if not name:
        return None
    try:
        from app.shared_excel import sharepoint_storage as sps

        if not sps.is_configured():
            return None
        downloaded = sps.download_manifest(name)
    except Exception:  # noqa: BLE001
        return None
    if downloaded is None or not downloaded.exists():
        return None
    try:
        folder = manifest_folder(target_date)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        if downloaded.resolve() != target.resolve():
            shutil.copy2(downloaded, target)
        return target
    except OSError:
        return downloaded


_NEW_FILENAME_RE = re.compile(
    r"^(?P<station>[A-Za-z0-9]{2,6})"
    r"\s*-\s*"
    r"(?:(?P<cycle>[A-Za-z0-9_]+?)\s*-\s*)?"
    r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})"
    r"(?P<ext>\.xls[mx]?)$",
    re.IGNORECASE,
)


_FILENAME_RE = re.compile(
    r"^PlanManifest"
    r"(?:_(?P<station>[A-Z0-9]{3,6}))?"
    r"(?:_(?P<cycle>[A-Z0-9_]+?))?"
    r"_(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})"
    r"(?P<ext>\.xls[mx]?)$",
    re.IGNORECASE,
)

_PM_FILENAME_RE = re.compile(
    r"^PM\s+(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})"
    r"(?P<ext>\.xls[mx]?)$",
    re.IGNORECASE,
)


def parse_manifest_filename(name: str) -> dict[str, str] | None:
    """Decompose a manifest filename into ``{station, cycle, day, month, year, ext}``.

    Handles the active layout (``STATION - CYCLE - DD.MM.YYYY.xlsx`` and
    ``STATION - DD.MM.YYYY.xlsx``) as well as the legacy
    ``PlanManifest_*`` and ``PM DD.MM.YYYY.xlsx`` names (read-only
    back-compat). Returns ``None`` when ``name`` matches nothing.
    """
    raw = str(name or "").strip()

    # 1) Active layout: ``STATION - [CYCLE - ]DD.MM.YYYY.ext``.
    match = _NEW_FILENAME_RE.match(raw)
    if match:
        parts = match.groupdict()
        return {
            "station": (parts.get("station") or "").upper(),
            "cycle": (parts.get("cycle") or "").upper(),
            "day": parts["day"].zfill(2),
            "month": parts["month"].zfill(2),
            "year": parts["year"],
            "ext": parts["ext"].lower(),
        }

    # 2) Legacy ``PlanManifest_*`` layout.
    match = _FILENAME_RE.match(raw)
    if match:
        parts = match.groupdict()
        return {
            "station": (parts.get("station") or "").upper(),
            "cycle": (parts.get("cycle") or "").upper(),
            "day": parts["day"].zfill(2),
            "month": parts["month"].zfill(2),
            "year": parts["year"],
            "ext": parts["ext"].lower(),
        }

    # 3) Legacy ``PM DD.MM.YYYY.ext`` layout (no station/cycle).
    match = _PM_FILENAME_RE.match(raw)
    if match:
        parts = match.groupdict()
        return {
            "station": "",
            "cycle": "",
            "day": parts["day"].zfill(2),
            "month": parts["month"].zfill(2),
            "year": parts["year"],
            "ext": parts["ext"].lower(),
        }

    return None


def list_daily_manifests(
    when: date | datetime | None = None,
    *,
    base_am_dir: str | None = None,  # noqa: A002
) -> list[dict[str, Any]]:
    """List every Plan Manifest file persisted for ``when``.

    Returns one entry per matching file with ``{path, name, station,
    cycle, size, modified_at}``. Useful for the operator panel that
    paints which (station, cycle) tuples are already covered.
    """
    target_date = _to_date(when)

    # Single source of truth: the Toolbox Manifest folder.
    out: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    suffix_set = {ext.lower() for ext in MANIFEST_EXTENSIONS}
    for folder in (manifest_folder(target_date),):
        if not folder.exists():
            continue
        for entry in folder.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in suffix_set:
                continue
            if entry.name in seen_names:
                continue
            meta = parse_manifest_filename(entry.name)
            if not meta:
                continue
            if (
                meta["day"] != f"{target_date.day:02d}"
                or meta["month"] != f"{target_date.month:02d}"
                or meta["year"] != str(target_date.year)
            ):
                continue
            try:
                stat = entry.stat()
                size = stat.st_size
                mtime = stat.st_mtime
            except OSError:
                size = 0
                mtime = 0.0
            seen_names.add(entry.name)
            out.append(
                {
                    "path": str(entry),
                    "name": entry.name,
                    "station": meta["station"],
                    "cycle": meta["cycle"],
                    "size": size,
                    "modified_at": mtime,
                }
            )
    out.sort(key=lambda r: (r["station"], r["cycle"], r["name"]))

    # Best-effort: fold in manifests that live on SharePoint for the
    # current DS but have not been pulled into the local cache yet
    # (fresh install, another operator uploaded them, ...). Each entry
    # is reported with the same shape; ``path`` points at the local
    # cache location where ``find_daily_manifest`` would land them on
    # first read.
    try:
        from app.shared_excel import sharepoint_storage as _sps

        if _sps.is_configured():
            for sp_entry in _sps.list_manifests():
                name = str(sp_entry.get("name") or "")
                if not name or name in seen_names:
                    continue
                if Path(name).suffix.lower() not in suffix_set:
                    continue
                meta = parse_manifest_filename(name)
                if not meta:
                    continue
                if (
                    meta["day"] != f"{target_date.day:02d}"
                    or meta["month"] != f"{target_date.month:02d}"
                    or meta["year"] != str(target_date.year)
                ):
                    continue
                local_target = manifest_folder(target_date) / name
                size = int(sp_entry.get("size") or 0)
                mtime = 0.0
                raw_mtime = str(sp_entry.get("time_last_modified") or "")
                if raw_mtime:
                    try:
                        from datetime import datetime as _dt

                        mtime = _dt.fromisoformat(
                            raw_mtime.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        mtime = 0.0
                seen_names.add(name)
                out.append(
                    {
                        "path": str(local_target),
                        "name": name,
                        "station": meta["station"],
                        "cycle": meta["cycle"],
                        "size": size,
                        "modified_at": mtime,
                        "source": "sharepoint",
                    }
                )
            out.sort(key=lambda r: (r["station"], r["cycle"], r["name"]))
    except Exception:  # noqa: BLE001 - never break the panel on auth failure
        pass

    return out


def canonical_manifest_path(
    when: date | datetime | None = None,
    *,
    base_am_dir: str | None = None,  # noqa: A002
    station_code: str | None = None,
    cycle: str | None = None,
) -> Path | None:
    """Return the path where the canonical persisted manifest lives.

    The file may not exist yet. Useful for ``persist_uploaded_manifest``.

    With ``station_code`` and ``cycle`` we return the most specific
    canonical filename (``<STATION> - <CYCLE> - DD.MM.YYYY.xlsx``).
    With ``station_code`` alone we drop the cycle segment. Without
    either we return the date-only name so deployments that never set
    ``STATION_CODE`` keep working.
    """
    base = _clean_env_path(base_am_dir if base_am_dir is not None else os.environ.get("BASE_AM_TB", ""))
    target_date = _to_date(when)
    folder = manifest_folder(target_date)
    if not str(folder.parent):  # defensive: no Toolbox base resolved
        return None
    station = _norm_station(station_code)
    cycle_norm = _norm_cycle(cycle)
    if station and cycle_norm:
        name = _format_full(station, cycle_norm, target_date, ".xlsx")
    elif station:
        name = _format_with_station(station, target_date, ".xlsx")
    else:
        name = _format_date_only(target_date, ".xlsx")
    return folder / name


def persist_uploaded_manifest(
    upload_path: Path | str,
    *,
    when: date | datetime | None = None,
    base_am_dir: str | None = None,  # noqa: A002
    overwrite: bool = False,
    station_code: str | None = None,
    cycle: str | None = None,
) -> Path | None:
    """Copy an uploaded manifest into the canonical shared location.

    * If ``BASE_AM_TB`` is not configured we return ``None``.
    * If a manifest is already present for that ``(station, cycle, day)``
      tuple we leave it alone unless ``overwrite=True``.
    * Returns the destination path on success, ``None`` on failure.

    The function is best-effort: any IO error is logged via the
    standard logger but does not propagate (the upload still works for
    the current run).
    """
    target_date = _to_date(when)
    if not isinstance(upload_path, Path):
        upload_path = Path(str(upload_path))
    if not upload_path.exists():
        return None

    folder = manifest_folder(target_date)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    station = _norm_station(station_code)
    cycle_norm = _norm_cycle(cycle)

    if not overwrite:
        existing = find_daily_manifest(
            target_date,
            station_code=station or None,
            cycle=cycle_norm or None,
        )
        if existing is not None:
            # When the existing file is less specific than what the
            # caller wants, treat it as missing so we DO promote the
            # upload to the more specific filename. Example: a date-only
            # ``08.06.2026.xlsx`` exists but caller asks for
            # ``(DCZ4, CYCLE_1)`` — write the new specific copy and let
            # both coexist.
            if station and station not in existing.name.upper():
                pass  # fall through and write the more specific name
            elif cycle_norm and cycle_norm.upper() not in existing.name.upper():
                pass
            else:
                return existing

    if station and cycle_norm:
        canonical_name = _format_full(station, cycle_norm, target_date, ".xlsx")
    elif station:
        canonical_name = _format_with_station(station, target_date, ".xlsx")
    else:
        canonical_name = _format_date_only(target_date, ".xlsx")

    canonical = folder / canonical_name
    try:
        shutil.copy2(upload_path, canonical)
    except OSError:
        return None
    _push_manifest_to_sharepoint(canonical)
    return canonical


def persist_manifest_bytes(
    content: bytes,
    *,
    when: date | datetime | None = None,
    base_am_dir: str | None = None,  # noqa: A002
    station_code: str | None = None,
    cycle: str | None = None,
    overwrite: bool = True,
) -> Path | None:
    """Persist raw manifest bytes (RoutingTools download) at the canonical path.

    Same naming rules as :func:`persist_uploaded_manifest`. Defaults to
    ``overwrite=True`` because the auto-fetch flow pulls a fresh copy
    on demand and we want it to win over a stale cached file.
    """
    target_date = _to_date(when)
    # Never persist an empty payload: a 0-byte file shows as "0 B" in
    # the panel and crashes any tool that tries to parse it as XLSX.
    if not content:
        return None
    folder = manifest_folder(target_date)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    station = _norm_station(station_code)
    cycle_norm = _norm_cycle(cycle)
    if station and cycle_norm:
        name = _format_full(station, cycle_norm, target_date, ".xlsx")
    elif station:
        name = _format_with_station(station, target_date, ".xlsx")
    else:
        name = _format_date_only(target_date, ".xlsx")

    target = folder / name
    if target.exists() and not overwrite:
        return target
    try:
        target.write_bytes(content)
    except OSError:
        return None
    _push_manifest_to_sharepoint(target)
    return target


def resolve_manifest_path(
    payload: dict[str, Any] | None,
    *,
    keys: Iterable[str] = ("manifest", "plan_manifest", "manifest_1"),
    when: date | datetime | None = None,
    persist: bool = True,
    overwrite: bool = False,
    station_code: str | None = None,
    cycle: str | None = None,
) -> tuple[Path | None, str]:
    """Pick the manifest a tool should consume.

    Returns ``(path, source)`` where ``source`` is one of ``"upload"``,
    ``"shared"`` or ``""`` (no manifest available).

    * ``"upload"`` — the user dragged a file. The file is also copied
      to the shared canonical location when ``persist`` is True, so
      every other tool finds it on the next call.
    * ``"shared"`` — no upload, but ``find_daily_manifest`` matched on
      disk for the requested ``(station_code, cycle, when)``.
    """
    file_paths: dict[str, Any] = {}
    if isinstance(payload, dict):
        raw = payload.get("file_paths")
        if isinstance(raw, dict):
            file_paths = raw

    upload_path: Path | None = None
    for key in keys:
        candidate = file_paths.get(key)
        if isinstance(candidate, list) and candidate:
            candidate = candidate[0]
        if not candidate and isinstance(payload, dict):
            candidate = payload.get(f"{key}_path") or payload.get(key)
        if not candidate:
            continue
        candidate_path = Path(str(candidate))
        if candidate_path.exists():
            upload_path = candidate_path
            break

    target_date = _to_date(when)
    if upload_path is not None:
        if persist:
            persist_uploaded_manifest(
                upload_path,
                when=target_date,
                overwrite=overwrite,
                station_code=station_code,
                cycle=cycle,
            )
        return upload_path, "upload"

    shared = find_daily_manifest(
        target_date,
        station_code=station_code,
        cycle=cycle,
    )
    if shared is not None:
        return shared, "shared"
    return None, ""


# ---------------------------------------------------------------------------
# Manifest readers (kept compatible with the legacy FTDS / Multi-Items
# implementations so callers can switch to this module without breaking).
# ---------------------------------------------------------------------------


_ROUTE_CODE_RE = re.compile(r"([A-Z]{2,5}_[A-Z]\d+)")
_OPENING_RE = re.compile(r"\bopening\b")
_WINDOW_RE = re.compile(r"\bwindow\b")


def _extract_route_code(value: object) -> str:
    text = str(value or "").upper()
    match = _ROUTE_CODE_RE.search(text)
    return match.group(1) if match else ""


def _find_manifest_col(columns: list[str], candidates: list[object]) -> int | None:
    """Mirror legacy ``FTDS._find_manifest_col``: substring match (case-
    insensitive) over column names; supports plain strings AND
    pre-compiled regex patterns.
    """
    norm = [str(c).strip().lower() for c in columns]
    for idx, col in enumerate(norm):
        for cand in candidates:
            if isinstance(cand, str):
                if cand and cand in col:
                    return idx
            else:
                try:
                    if cand.search(col):
                        return idx
                except Exception:
                    continue
    return None


def load_manifest_context(path: Path | None) -> dict[str, dict[str, str]]:
    """Return ``tid -> {"route", "schedule"}`` from a Plan Manifest.

    Mirrors the legacy FTDS ``load_manifest_tracking_context`` helper:

    * Tracking column candidates are matched as substrings (so columns
      like ``"Tracking Id (TBA…)"`` still resolve).
    * Commercial-schedule columns are detected via legacy candidates
      (``horario comercial``, ``horario de apertura``, ``time window``,
      ``opening hours``, ``delivery window``) plus regex fallbacks for
      ``opening`` / ``window``. Generic shipping fields like
      ``ship option`` / ``service type`` are intentionally NOT
      candidates because they leak non-time text into the schedule and
      break the downstream business-opportunity check.
    """
    if not path or not Path(path).exists():
        return {}
    try:
        xls = pd.ExcelFile(path)
    except Exception:  # noqa: BLE001
        return {}

    tid_candidates: list[object] = [
        "tracking id",
        "trackingid",
        "tracking_id",
        "tracking",
        "shipment id",
        "package id",
    ]
    sched_candidates: list[object] = [
        "horario de apertura",
        "horario comercial",
        "time window",
        "opening hours",
        "delivery window",
        "horario",
        _OPENING_RE,
        _WINDOW_RE,
    ]

    context: dict[str, dict[str, str]] = {}
    for sheet in xls.sheet_names:
        try:
            raw = xls.parse(sheet, header=None)
        except Exception:  # noqa: BLE001
            continue
        if raw.empty or raw.shape[0] < 3:
            continue

        route = _extract_route_code(raw.iat[0, 0]) if raw.shape[1] > 0 else ""
        if not route:
            route = _extract_route_code(sheet)
        columns = [str(c) for c in raw.iloc[1].tolist()]

        idx_tid = _find_manifest_col(columns, tid_candidates)
        if idx_tid is None:
            continue
        idx_sched = _find_manifest_col(columns, sched_candidates)

        body = raw.iloc[2:].reset_index(drop=True)
        for _, row in body.iterrows():
            tid = str(row.iloc[idx_tid] if idx_tid < len(row) else "").strip()
            if not tid:
                continue
            schedule = ""
            if idx_sched is not None and idx_sched < len(row):
                raw_sched = row.iloc[idx_sched]
                schedule = "" if pd.isna(raw_sched) else str(raw_sched).strip()
                if schedule.lower() in {"nan", "none", "null"}:
                    schedule = ""
            entry = context.setdefault(tid, {"route": route, "schedule": schedule})
            if route and not entry.get("route"):
                entry["route"] = route
            if schedule and not entry.get("schedule"):
                entry["schedule"] = schedule
    return context


def load_manifest_route_dsp_context(source: Path | Any) -> dict[str, dict[str, str]]:
    """Return ``tid -> {"route", "dsp"}`` from a Plan Manifest.

    Same sheet walk as :func:`load_manifest_context`, but instead of the
    commercial schedule it resolves the DSP short code (abbreviation)
    per sheet: the RoutingTools export embeds the DSP's Atlas UUID in
    the sheet header, and the Horarios helpers map it to the configured
    ``DSP_ID_<CODE>`` env entries. Routes without a recognizable UUID
    come back with ``dsp=""`` (the caller keeps its own fallback).

    ``source`` can be a filesystem path or a binary file-like object
    (uploaded manifest) — anything ``pandas.ExcelFile`` accepts.
    """
    if source is None:
        return {}
    if isinstance(source, (str, Path)) and not Path(source).exists():
        return {}
    try:
        xls = pd.ExcelFile(source)
    except Exception:  # noqa: BLE001
        return {}

    # Lazy import: the Horarios module owns the DSP-UUID mapping logic
    # (importing it at module level would be circular for its callers).
    try:
        from app.core.env_loader import parse_allowed_dsps
        from app.tools.common.horarios import (
            _build_dsp_id_map,
            _dsp_from_manifest_id,
            _extract_manifest_dsp_id,
        )

        allowed = parse_allowed_dsps(os.environ.get("ALLOWED_DSPS", ""))
        dsp_id_map = _build_dsp_id_map(list(allowed))
    except Exception:  # noqa: BLE001
        dsp_id_map = {}
        _extract_manifest_dsp_id = None  # type: ignore[assignment]
        _dsp_from_manifest_id = None  # type: ignore[assignment]

    tid_candidates: list[object] = [
        "tracking id",
        "trackingid",
        "tracking_id",
        "tracking",
        "shipment id",
        "package id",
    ]

    context: dict[str, dict[str, str]] = {}
    for sheet in xls.sheet_names:
        try:
            raw = xls.parse(sheet, header=None)
        except Exception:  # noqa: BLE001
            continue
        if raw.empty or raw.shape[0] < 3:
            continue

        route = _extract_route_code(raw.iat[0, 0]) if raw.shape[1] > 0 else ""
        if not route:
            route = _extract_route_code(sheet)

        dsp = ""
        if dsp_id_map and _extract_manifest_dsp_id and _dsp_from_manifest_id:
            try:
                mapped = _dsp_from_manifest_id(_extract_manifest_dsp_id(raw), dsp_id_map)
                if mapped and mapped != "UNASSIGNED":
                    dsp = mapped
            except Exception:  # noqa: BLE001
                dsp = ""

        columns = [str(c) for c in raw.iloc[1].tolist()]
        idx_tid = _find_manifest_col(columns, tid_candidates)
        if idx_tid is None:
            continue

        body = raw.iloc[2:].reset_index(drop=True)
        for _, row in body.iterrows():
            tid = str(row.iloc[idx_tid] if idx_tid < len(row) else "").strip()
            if not tid or tid.lower() in {"nan", "none", "null", "0"}:
                continue
            entry = context.setdefault(tid.upper(), {"route": route, "dsp": dsp})
            if route and not entry.get("route"):
                entry["route"] = route
            if dsp and not entry.get("dsp"):
                entry["dsp"] = dsp
    return context


def collect_manifest_tracking_ids(path: Path | str) -> list[str]:
    """Return the flat (deduplicated) list of tracking IDs in a manifest."""
    target = Path(str(path)) if not isinstance(path, Path) else path
    if not target.exists():
        return []

    try:
        xls = pd.ExcelFile(target)
    except Exception:  # noqa: BLE001
        return []

    seen: set[str] = set()
    out: list[str] = []
    for sheet in xls.sheet_names:
        try:
            raw = xls.parse(sheet, header=None)
        except Exception:  # noqa: BLE001
            continue
        if raw.empty or raw.shape[0] < 3:
            continue
        columns = [str(c) for c in raw.iloc[1].tolist()]
        idx_tid = _find_manifest_col(
            columns,
            [
                "tracking id",
                "trackingid",
                "tracking_id",
                "tracking",
                "shipment id",
                "package id",
            ],
        )
        if idx_tid is None:
            continue

        body = raw.iloc[2:]
        for _, row in body.iterrows():
            if idx_tid >= len(row):
                continue
            tid = str(row.iloc[idx_tid] or "").strip()
            if not tid or tid in seen:
                continue
            seen.add(tid)
            out.append(tid)
    return out


# ---------------------------------------------------------------------------
# SharePoint sync (best-effort, short retention)
# ---------------------------------------------------------------------------
# Plan Manifests are also pushed to ``<library>/<STATION>/Manifest/`` on
# SharePoint so any DS can fetch the file even when the local cache is
# empty (fresh install, different machine, ...). We keep only the last
# N days on SharePoint to avoid unbounded growth (default: 2 days). All
# operations are best-effort: failures never block the local write.


def _push_manifest_to_sharepoint(local_path: Path) -> None:
    """Best-effort upload of a freshly-persisted Plan Manifest.

    Reads ``local_path`` and pushes it to the per-DS Manifest folder on
    SharePoint, then prunes manifests older than the retention window.
    Any failure (SharePoint not configured, auth expired, network
    error) is swallowed so the local write keeps working offline.
    """
    try:
        from app.shared_excel import sharepoint_storage as sps
    except Exception:  # noqa: BLE001
        return
    try:
        if not sps.is_configured():
            return
        if not local_path.exists():
            return
        content = local_path.read_bytes()
        if not content:
            return
        sps.upload_manifest(local_path.name, content)
        sps.prune_old_manifests()
    except Exception:  # noqa: BLE001 - best-effort, never break callers
        return


def _try_download_manifest_from_sharepoint(
    target_date: date,
    *,
    station: str,
    cycle: str = "",
) -> Path | None:
    """Best-effort download of a Plan Manifest from SharePoint.

    Tries the most specific filename first (station + cycle), falling
    back to station-only, then date-only. Returns the local cache path
    where the file landed, or ``None`` if nothing matched / SharePoint
    is unreachable.
    """
    try:
        from app.shared_excel import sharepoint_storage as sps
    except Exception:  # noqa: BLE001
        return None
    try:
        if not sps.is_configured():
            return None
        candidates: list[str] = []
        if station and cycle:
            for ext in MANIFEST_EXTENSIONS:
                candidates.append(_format_full(station, cycle, target_date, ext))
        if station:
            for ext in MANIFEST_EXTENSIONS:
                candidates.append(_format_with_station(station, target_date, ext))
        for ext in MANIFEST_EXTENSIONS:
            candidates.append(_format_date_only(target_date, ext))
        # Dedupe while preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for name in candidates:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        local_folder = manifest_folder(target_date)
        try:
            local_folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        for name in ordered:
            try:
                downloaded = sps.download_manifest(name)
            except Exception:  # noqa: BLE001
                downloaded = None
            if downloaded is None:
                continue
            # ``download_manifest`` writes to the SP cache mirror; copy
            # it into the manifest_lookup canonical location so the
            # existing on-disk lookups find it too.
            try:
                target = local_folder / name
                if downloaded.resolve() != target.resolve():
                    shutil.copy2(downloaded, target)
                return target
            except OSError:
                return downloaded
    except Exception:  # noqa: BLE001
        return None
    return None


__all__ = [
    "MANIFEST_EXTENSIONS",
    "CANONICAL_TEMPLATE",
    "CANONICAL_TEMPLATE_WITH_STATION",
    "CANONICAL_TEMPLATE_FULL",
    "base_am_dir",
    "month_folder",
    "manifest_folder",
    "find_daily_manifest",
    "list_daily_manifests",
    "canonical_manifest_path",
    "persist_uploaded_manifest",
    "persist_manifest_bytes",
    "resolve_manifest_path",
    "load_manifest_context",
    "collect_manifest_tracking_ids",
    "parse_manifest_filename",
]
