"""Silent daily persistence of Horizon baselines rates to SharePoint.

Horizon exposes its planning rates one week ahead (``week +1``). This module
runs a *silent* daily sync: it pulls the process-path rates for every day in
the upcoming window (:func:`app.integrations.horizon_client.fetch_rates_window`)
and appends them to a per-year workbook on SharePoint:

    <STATION>/<ISO-year>/Horizon/Wk<first>-<last>.xlsx   (e.g. Wk30-42.xlsx)

The workbook has **one worksheet per ISO week** (``Wk30``, ``Wk31`` …) and one
row per day inside it, with a column per Horizon metric. Re-running the same
day upserts the day's row (keeps the latest known rates). If the ``Horizon``
folder does not exist it is created (``station_cache_dir`` mkdirs the tree).

A once-per-day guard (a ``.last_sync`` marker under ``<STATION>/Horizon``)
means the fetch only hits Horizon once per calendar day per station unless
``force=True`` is passed.
"""
from __future__ import annotations

import logging
import os
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.watchdog import watchdog_pause
from app.integrations.horizon_client import fetch_rates_window

LOG = logging.getLogger("toolbox.horizon_metrics")

WORKBOOK_NAME = "Semanas.xlsx"  # fallback when the week range can't be derived
HORIZON_FOLDER = "Horizon"


def _horizon_week(d: date) -> tuple[int, int]:
    """Return ``(year, week)`` using Horizon's week convention, not ISO's.

    Horizon weeks run **Sunday..Saturday** and are labelled ``WK<n>`` where
    ``n`` is the ISO week of that week's Monday. Python's ``isocalendar()``
    uses Monday-start weeks, so a Sunday would fall in the *previous* ISO week
    (e.g. Sun 2026-07-19 -> ISO wk29) and split Horizon's WK30 across two
    sheets. Shifting Sunday to its following Monday keeps each Horizon week
    intact and matching the labels shown in the Inputs Editor.
    """
    ref = d + timedelta(days=1) if d.weekday() == 6 else d
    iso = ref.isocalendar()
    return iso[0], iso[1]


def _workbook_name(year_days: dict[str, tuple[int, dict]]) -> str:
    """Name the workbook after the ISO-week range it covers, e.g. ``Wk30-42.xlsx``.

    A single week yields ``Wk30.xlsx``; an empty set falls back to
    :data:`WORKBOOK_NAME`.
    """
    weeks = sorted({wk for (wk, _) in year_days.values()})
    if not weeks:
        return WORKBOOK_NAME
    if len(weeks) == 1:
        return f"Wk{weeks[0]:02d}.xlsx"
    return f"Wk{weeks[0]:02d}-{weeks[-1]:02d}.xlsx"
_MARKER_NAME = ".last_sync.txt"

# ---------------------------------------------------------------------------
# Once-per-day guard
# ---------------------------------------------------------------------------
def _marker_path() -> Path | None:
    try:
        from app.shared_excel import sharepoint_storage as sps

        return sps.station_cache_dir(HORIZON_FOLDER) / _MARKER_NAME
    except Exception:  # noqa: BLE001
        return None


def _already_ran_today() -> bool:
    path = _marker_path()
    if path is None or not path.exists():
        return False
    try:
        return path.read_text(encoding="utf-8").strip() == date.today().isoformat()
    except OSError:
        return False


def _mark_ran_today() -> None:
    path = _marker_path()
    if path is None:
        return
    try:
        path.write_text(date.today().isoformat(), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Workbook helpers
# ---------------------------------------------------------------------------
def _atomic_save_workbook(wb, target: Path) -> None:  # type: ignore[no-untyped-def]
    """Save ``.tmp`` then atomically replace, keeping a ``.bak`` fallback."""
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    tmp_path = target.with_suffix(target.suffix + ".tmp")
    backup_path = target.with_suffix(target.suffix + ".bak") if target.exists() else None

    if backup_path:
        try:
            shutil.copy2(target, backup_path)
        except OSError:
            backup_path = None

    saved = False
    try:
        wb.save(tmp_path)
        os.replace(tmp_path, target)
        saved = True
        try:
            load_workbook(target).close()
        except InvalidFileException:
            if backup_path and backup_path.exists():
                shutil.copy2(backup_path, target)
            raise
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        if backup_path is not None and backup_path.exists():
            try:
                if saved:
                    backup_path.unlink()
            except OSError:
                pass


def _write_year_workbook(target: Path, year_days: dict[str, tuple[int, dict[str, float]]]) -> int:
    """Open/create ``target`` and write each ISO week into its ``Wk<week>`` sheet.

    ``year_days`` maps ``date_iso -> (iso_week, raw_metrics)``. Each week sheet
    reproduces Horizon's native layout **exactly**: one row per metric and one
    column per day (``metric_name`` header + the dates of that week), with the
    values stored verbatim (no rounding, no fabricated values; a blank cell
    means Horizon returned no value for that metric/day). A week already
    present in the workbook is fully rebuilt (overwritten) so it always
    matches the latest Horizon download; weeks from previous runs that are no
    longer in the window are left untouched (historical archive).

    Returns the number of day-columns written across all weeks.
    """
    from openpyxl import Workbook, load_workbook

    if target.exists():
        try:
            wb = load_workbook(target)
        except Exception:  # noqa: BLE001 - corrupted file: start fresh
            wb = Workbook()
            wb.remove(wb.active)
    else:
        wb = Workbook()
        wb.remove(wb.active)  # drop the default empty sheet

    # Group the year's days by ISO week.
    weeks: dict[int, dict[str, dict[str, float]]] = defaultdict(dict)
    for date_iso, (iso_week, raw) in year_days.items():
        weeks[iso_week][date_iso] = raw

    written = 0
    for iso_week, day_map in weeks.items():
        sheet_name = f"Wk{iso_week:02d}"
        # Overwrite: rebuild the sheet from scratch so it matches the current
        # Horizon download exactly (no stale rows/columns from a prior run).
        if sheet_name in wb.sheetnames:
            del wb[sheet_name]
        ws = wb.create_sheet(sheet_name)

        dates_sorted = sorted(day_map)
        # Preserve Horizon's metric order (the raw dict keeps the response's
        # row order), matching what the operator sees in the Inputs Editor.
        metric_order: list[str] = []
        seen: set[str] = set()
        for d in dates_sorted:
            for metric in day_map[d]:
                if metric not in seen:
                    seen.add(metric)
                    metric_order.append(metric)

        # Header: metric_name + one column per date (Horizon's native shape).
        ws.cell(row=1, column=1, value="metric_name")
        for col, d in enumerate(dates_sorted, start=2):
            ws.cell(row=1, column=col, value=d)
        # One row per metric with the exact Horizon value for each day.
        for row, metric in enumerate(metric_order, start=2):
            ws.cell(row=row, column=1, value=metric)
            for col, d in enumerate(dates_sorted, start=2):
                value = day_map[d].get(metric)
                if value is not None:
                    ws.cell(row=row, column=col, value=value)
        written += len(dates_sorted)

    # Order sheets by week number.
    try:
        wb._sheets.sort(key=lambda s: _sheet_sort_key(s.title))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass

    _atomic_save_workbook(wb, target)
    return written


def _sheet_sort_key(title: str) -> tuple[int, str]:
    if title.lower().startswith("wk"):
        try:
            return (int(title[2:]), title)
        except ValueError:
            return (9999, title)
    return (10000, title)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_daily_sync(
    *,
    station: str,
    force: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Fetch the upcoming Horizon rates and persist them to SharePoint.

    Returns a status dict: ``{"saved": bool, "skipped": bool, "reason": str,
    "days": int, "files": [...], "date": "YYYY-MM-DD"}``. Auth/HTTP failures
    are raised (``HorizonAuthError`` / ``HorizonError``) for the route layer.
    """
    station = (station or "").strip().upper()
    if not station or station == "__ALL__":
        return {"saved": False, "skipped": True, "reason": "no_station", "days": 0, "files": []}

    if not force and _already_ran_today():
        return {
            "saved": False,
            "skipped": True,
            "reason": "already_ran_today",
            "days": 0,
            "files": [],
            "date": date.today().isoformat(),
        }

    window = fetch_rates_window(station=station, start_date=start_date, end_date=end_date)
    days: dict[str, Any] = window.get("days") or {}
    if not days:
        # Nothing to persist, but count the run so we don't hammer Horizon.
        _mark_ran_today()
        return {"saved": False, "skipped": True, "reason": "no_data", "days": 0, "files": []}

    # Group days by ISO year; each lands in <STATION>/<year>/Horizon/Wk<a>-<b>.xlsx.
    by_year: dict[int, dict[str, tuple[int, dict[str, float]]]] = defaultdict(dict)
    for date_iso, payload in days.items():
        try:
            d = date.fromisoformat(date_iso)
        except ValueError:
            continue
        hz_year, hz_week = _horizon_week(d)
        by_year[hz_year][date_iso] = (hz_week, payload.get("raw") or {})

    from app.shared_excel import sharepoint_storage as sps

    files: list[str] = []
    total_rows = 0
    with watchdog_pause():
        for year, year_days in by_year.items():
            folder = sps.station_cache_dir(str(year), HORIZON_FOLDER)
            target = Path(folder) / _workbook_name(year_days)
            total_rows += _write_year_workbook(target, year_days)
            files.append(str(target))
            # Push each per-year workbook back to SharePoint when configured.
            try:
                if sps.is_configured():
                    sps.push_file(target)
            except Exception:  # noqa: BLE001 - never fail the sync on upload
                LOG.info("horizon metrics upload skipped for %s", target)

    _mark_ran_today()
    return {
        "saved": True,
        "skipped": False,
        "reason": "",
        "days": total_rows,
        "files": files,
        "date": date.today().isoformat(),
        "station": station,
        "window": {"start": window.get("start"), "end": window.get("end")},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def read_stored_rates(*, station: str, date_iso: str) -> dict[str, Any]:
    """Read the rates stored for a single day from the SharePoint workbook.

    Locates the ``Wk<week>`` sheet for ``date_iso`` (across whatever
    ``Wk<a>-<b>.xlsx`` files exist for that ISO year), reads the day's column
    and maps the Horizon metric names onto the Labor Calculator's field names.

    Returns ``{"found": bool, "station", "date", "raw",
    "suggested_sources", "ns_rates", "ns_params", "ampt_rates",
    "ampt_params"}``. ``found`` is ``False`` when there is no column for
    that day (the caller should then fall back to default rates).
    """
    from openpyxl import load_workbook

    from app.integrations.horizon_client import (
        AMPT_PARAM_MAP,
        AMPT_RATE_MAP,
        NS_PARAM_MAP,
        NS_RATE_MAP,
    )
    from app.shared_excel import sharepoint_storage as sps

    station = (station or "").strip().upper()
    empty = {
        "found": False,
        "station": station,
        "date": date_iso,
        "raw": {},
        "suggested_sources": {},
        "ns_rates": {},
        "ns_params": {},
        "ampt_rates": {},
        "ampt_params": {},
    }
    try:
        d = date.fromisoformat(date_iso)
    except ValueError:
        return empty

    hz_year, hz_week = _horizon_week(d)
    sheet_name = f"Wk{hz_week:02d}"

    # Prefer freshly-synced files from SharePoint; fall back to the local cache.
    paths: list[Path] = []
    try:
        paths = [Path(p) for p in sps.pull_folder([str(hz_year), HORIZON_FOLDER], suffixes={".xlsx"})]
    except Exception:  # noqa: BLE001
        paths = []
    if not paths:
        folder = sps.station_cache_dir(str(hz_year), HORIZON_FOLDER)
        paths = sorted(Path(folder).glob("*.xlsx"))

    raw: dict[str, float] = {}
    for path in paths:
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception:  # noqa: BLE001 - skip unreadable files
            continue
        try:
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
            col_idx = next((i for i, val in enumerate(header) if str(val) == date_iso), None)
            if col_idx is None:
                continue
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row:
                    continue
                metric = row[0]
                if metric in (None, ""):
                    continue
                value = row[col_idx] if col_idx < len(row) else None
                if value is None or value == "":
                    continue
                try:
                    raw[str(metric)] = float(value)
                except (TypeError, ValueError):
                    continue
        finally:
            wb.close()
        if raw:
            break

    if not raw:
        return empty

    def _mapped(mapping: dict[str, str]) -> dict[str, float]:
        return {label: raw[metric] for metric, label in mapping.items() if metric in raw}

    def _sources(mapping: dict[str, str]) -> dict[str, str]:
        return {label: metric for metric, label in mapping.items() if metric in raw}

    return {
        "found": True,
        "station": station,
        "date": date_iso,
        "raw": raw,
        "suggested_sources": {
            **_sources(NS_RATE_MAP),
            **_sources(NS_PARAM_MAP),
            **_sources(AMPT_RATE_MAP),
            **_sources(AMPT_PARAM_MAP),
        },
        "ns_rates": _mapped(NS_RATE_MAP),
        "ns_params": _mapped(NS_PARAM_MAP),
        "ampt_rates": _mapped(AMPT_RATE_MAP),
        "ampt_params": _mapped(AMPT_PARAM_MAP),
    }


__all__ = ["read_stored_rates", "run_daily_sync"]
