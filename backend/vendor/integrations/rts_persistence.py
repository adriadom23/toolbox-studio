"""Shared RTS persistence helpers.

Every time the FTDS / RTS tool runs in RTS mode we persist the
parsed records under ``BASE_DO_TB`` so the Daily Meeting tool can
auto-discover them on D-1 without forcing the operator to upload the
file twice. Two artefacts are kept:

* ``<BASE_DO_TB>/<year>/<MM. month>/RTS/RTS_<YYYY-MM-DD>.xlsx`` — daily
  slice in the legacy schema the Daily engine consumes.
* ``<BASE_DO_TB>/RTS/RTS_acumulado.xlsx`` — single global cumulative
  workbook with the same schema plus a ``ReportDate`` column. Each
  call removes any rows whose ``ReportDate`` matches the current run
  (so re-executing the same day stays idempotent) and trims the file
  to the last 90 days to keep it bounded.

The functions silently no-op when ``BASE_DO_TB`` is not configured
(matches the manifest_lookup contract).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

_log = logging.getLogger("toolbox.rts_persistence")


# Spanish month folder names (same convention as DSC / manifest_lookup).
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

# Schema the Daily engine's ``load_rts_new_schema`` consumes. Stored
# in this exact order so the file matches the legacy layout that the
# operator manually exported from FTDS V2.
_LEGACY_COLUMNS: tuple[str, ...] = (
    "DSP",
    "DSP_Short",
    "TrackingId",
    "Route",
    "Reason",
    "HadContact",
    "HasPHR",
    "PHR",
)

# How long to keep history in the cumulative workbook.
_CUMULATIVE_DAYS = 90


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _clean_env_path(raw: object) -> str:
    if not isinstance(raw, str):
        raw = str(raw or "")
    value = raw.strip().strip('"').strip("'")
    return re.sub(r"\\\\+", r"\\", value)


def _to_date(when: date | datetime | None) -> date:
    if when is None:
        return date.today()
    if isinstance(when, datetime):
        return when.date()
    return when


def base_do_dir() -> str:
    """Return the cleaned ``BASE_DO_TB`` env path."""
    return _clean_env_path(os.environ.get("BASE_DO_TB", ""))


def _month_folder(base: str | Path, when: date) -> Path:
    base_path = Path(_clean_env_path(str(base)))
    return base_path / str(when.year) / f"{when.month:02d}. {_MONTHS_ES[when.month - 1]}"


def daily_rts_path(when: date | datetime | None = None, *, base: str | None = None) -> Path | None:
    """Return ``<BASE_DO_TB>/<year>/<MM. month>/RTS/RTS_<YYYY-MM-DD>.xlsx``."""
    base_dir = _clean_env_path(base if base is not None else base_do_dir())
    if not base_dir:
        return None
    target = _to_date(when)
    folder = _month_folder(base_dir, target) / "RTS"
    return folder / f"RTS_{target.isoformat()}.xlsx"


def cumulative_rts_path(*, base: str | None = None) -> Path | None:
    """Return ``<BASE_DO_TB>/RTS/RTS_acumulado.xlsx``."""
    base_dir = _clean_env_path(base if base is not None else base_do_dir())
    if not base_dir:
        return None
    return Path(base_dir) / "RTS" / "RTS_acumulado.xlsx"


# ---------------------------------------------------------------------------
# DataFrame builder
# ---------------------------------------------------------------------------


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def legacy_rts_dataframe(records: Iterable[Any], *, with_transporter: bool = False) -> pd.DataFrame:
    """Convert FTDS / RTS dataclass records into the Daily-friendly schema.

    Accepts both :class:`app.tools.pm.ftds_rts.FtdsRecord` instances and
    plain dicts (``record.to_dict()``). The output uses the column
    names ``load_rts_new_schema`` expects. When ``with_transporter`` is
    set the returned frame also carries a ``Transporter`` column at the
    end — used by the cumulative workbook so the operator can audit
    which driver the return is tied to.
    """
    rows: list[dict[str, Any]] = []
    for raw in records or []:
        if hasattr(raw, "to_dict"):
            data = raw.to_dict()
        elif isinstance(raw, dict):
            data = raw
        else:
            continue

        row = {
            "DSP": _coerce_str(data.get("dsp_long") or data.get("dsp")),
            "DSP_Short": _coerce_str(data.get("dsp")),
            "TrackingId": _coerce_str(data.get("tracking")),
            "Route": _coerce_str(data.get("route")),
            "Reason": _coerce_str(data.get("reason")),
            "HadContact": bool(data.get("has_call")),
            "HasPHR": bool(data.get("has_phr")),
            "PHR": _coerce_str(data.get("phr")),
        }
        if with_transporter:
            row["Transporter"] = _coerce_str(data.get("transporter"))
        rows.append(row)
    if not rows:
        cols = list(_LEGACY_COLUMNS) + (["Transporter"] if with_transporter else [])
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    cols = list(_LEGACY_COLUMNS) + (["Transporter"] if with_transporter else [])
    return df[cols]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _safe_write_xlsx(df: pd.DataFrame, target: Path) -> bool:
    """Write ``df`` to ``target`` atomically. Returns True on success."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log.warning("RTS persistence: cannot create %s (%s)", target.parent, exc)
        return False

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        df.to_excel(tmp, index=False, engine="openpyxl")
    except Exception:
        _log.exception("RTS persistence: failed to write %s", tmp)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        # Atomic on the same filesystem; ``replace`` handles existing target.
        os.replace(tmp, target)
    except OSError:
        _log.exception("RTS persistence: failed to swap %s -> %s", tmp, target)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    # RTS daily/cumulative are LOCAL-only now: the shared store keeps only
    # the daily workbook (Daily reads the "FTDS RTS" sheet). Not uploaded.
    return True


def _push_to_sharepoint(path: Path) -> None:
    """Best-effort upload of a freshly-written cache file to SharePoint."""
    try:
        from app.shared_excel import sharepoint_storage as sps

        if sps.is_configured():
            sps.push_file(path)
    except Exception:  # noqa: BLE001 - never break the local write
        _log.exception("RTS persistence: SharePoint upload failed for %s", path)


def _pull_from_sharepoint(path: Path) -> None:
    """Best-effort download of a cache file from SharePoint before reading."""
    try:
        from app.shared_excel import sharepoint_storage as sps

        if sps.is_configured():
            sps.pull_cache_path(path)
    except Exception:  # noqa: BLE001 - offline reads use the cached copy
        pass


def _read_existing(path: Path) -> pd.DataFrame:
    """Best-effort load of the cumulative workbook. Returns an empty DF on failure."""
    if not path.exists():
        return pd.DataFrame(columns=list(_LEGACY_COLUMNS) + ["ReportDate", "Transporter"])
    try:
        df = pd.read_excel(path, dtype=str)
    except Exception:
        _log.exception("RTS persistence: cumulative file unreadable, rebuilding")
        return pd.DataFrame(columns=list(_LEGACY_COLUMNS) + ["ReportDate", "Transporter"])
    # Normalise booleans and dates.
    for col in ("HadContact", "HasPHR"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lower().isin(
                {"true", "1", "yes", "y", "si", "sí"}
            )
        else:
            df[col] = False
    if "ReportDate" not in df.columns:
        df["ReportDate"] = ""
    return df


def _trim_to_window(df: pd.DataFrame, *, days: int = _CUMULATIVE_DAYS, today: date | None = None) -> pd.DataFrame:
    """Drop rows whose ``ReportDate`` is older than ``days`` ago."""
    if df.empty or "ReportDate" not in df.columns:
        return df
    today = today or date.today()
    cutoff = today - timedelta(days=days - 1)
    cutoff_iso = cutoff.isoformat()
    # ReportDate is stored as ISO ``YYYY-MM-DD``; lexicographic compare works.
    keep = df["ReportDate"].astype(str).str.strip() >= cutoff_iso
    return df[keep].reset_index(drop=True)


def persist_rts(
    records: Iterable[Any],
    when: date | datetime,
    *,
    base: str | None = None,
) -> dict[str, Path | str]:
    """Write the daily slice + update the cumulative workbook.

    Returns a dict with ``daily``, ``cumulative`` and ``error`` keys.
    Empty values mean the artefact was not produced (no records, no
    BASE_DO_TB, or a write error). The function never raises.
    """
    out: dict[str, Path | str] = {"daily": "", "cumulative": "", "error": ""}
    target_date = _to_date(when)
    base_dir = _clean_env_path(base if base is not None else base_do_dir())
    if not base_dir:
        out["error"] = "BASE_DO_TB no configurado."
        return out

    df = legacy_rts_dataframe(records)
    if df.empty:
        out["error"] = "Sin registros RTS para persistir."
        return out

    daily_target = daily_rts_path(target_date, base=base_dir)
    if daily_target is not None and _safe_write_xlsx(df, daily_target):
        out["daily"] = daily_target

    # Cumulative workbook: dedup by date, append today, trim to window.
    cumulative_target = cumulative_rts_path(base=base_dir)
    if cumulative_target is None:
        return out

    # Build the cumulative slice with the extra ``Transporter`` column
    # so the operator can audit which driver each return is tied to
    # without opening Harmony manually. The daily file keeps the
    # legacy schema untouched (Daily's loader does not look for
    # Transporter, only the cumulative needs it).
    cumulative_df = legacy_rts_dataframe(records, with_transporter=True)

    existing = _read_existing(cumulative_target)
    if not existing.empty and "ReportDate" in existing.columns:
        existing = existing[
            existing["ReportDate"].astype(str).str.strip() != target_date.isoformat()
        ]

    today_slice = cumulative_df.copy()
    today_slice["ReportDate"] = target_date.isoformat()

    # Reorder + align columns so concat is clean. Transporter goes at
    # the very end so the legacy reader keeps working when it ignores
    # extra trailing columns.
    desired = list(_LEGACY_COLUMNS) + ["ReportDate", "Transporter"]
    for col in desired:
        if col not in today_slice.columns:
            today_slice[col] = "" if col != "ReportDate" else target_date.isoformat()
        if col not in existing.columns:
            existing[col] = ""
    existing = existing[desired]
    today_slice = today_slice[desired]

    merged = pd.concat([existing, today_slice], ignore_index=True)
    merged = _trim_to_window(merged, today=target_date)

    if _safe_write_xlsx(merged, cumulative_target):
        out["cumulative"] = cumulative_target
    return out


# ---------------------------------------------------------------------------
# Daily auto-discovery
# ---------------------------------------------------------------------------


def find_daily_rts(when: date | datetime | None = None, *, base: str | None = None) -> Path | None:
    """Return the path Daily should consume for ``when``.

    Lookup order:

    1. ``<BASE_DO_TB>/<year>/<MM. month>/RTS/RTS_<YYYY-MM-DD>.xlsx`` —
       direct daily file.
    2. Slice for ``when`` extracted from the cumulative workbook,
       written to a transient ``RTS_<YYYY-MM-DD>_from_cumulative.xlsx``
       under the same monthly folder so the engine has a real file to
       read. Avoids caller-side parsing.

    Returns ``None`` when neither artefact has rows for the requested
    day.
    """
    target_date = _to_date(when)
    base_dir = _clean_env_path(base if base is not None else base_do_dir())
    if not base_dir:
        return None

    direct = daily_rts_path(target_date, base=base_dir)
    if direct is not None:
        _pull_from_sharepoint(direct)
        if direct.exists():
            return direct

    cumulative = cumulative_rts_path(base=base_dir)
    if cumulative is not None:
        _pull_from_sharepoint(cumulative)
    if cumulative is None or not cumulative.exists():
        return None

    try:
        df = pd.read_excel(cumulative, dtype=str)
    except Exception:
        _log.exception("RTS persistence: failed to read cumulative file %s", cumulative)
        return None

    if "ReportDate" not in df.columns or df.empty:
        return None
    iso = target_date.isoformat()
    slice_df = df[df["ReportDate"].astype(str).str.strip() == iso].copy()
    if slice_df.empty:
        return None
    # Write a transient daily file so the Daily engine has a stable
    # path to read. Use the canonical daily name so subsequent runs
    # find it directly without rebuilding from the cumulative.
    slice_df = slice_df[[c for c in _LEGACY_COLUMNS if c in slice_df.columns]]
    target = daily_rts_path(target_date, base=base_dir)
    if target is None:
        return None
    if _safe_write_xlsx(slice_df, target):
        return target
    return None


__all__ = [
    "base_do_dir",
    "cumulative_rts_path",
    "daily_rts_path",
    "find_daily_rts",
    "legacy_rts_dataframe",
    "persist_rts",
]
