"""Loader for the Dive Deep Tool metric catalog.

The canonical store is the station's CONFIG WORKBOOK (SharePoint):
key ``DIVE_DEEP_CATALOG`` on the hidden KEY/VALUE sheet ``Dive_Deep`` —
same mechanism as the shift tasks (``Tareas_Turnos``). Edits made from
Configuración ▸ Dive Deep persist there, travel with the station and
survive exe upgrades.

Fallback (dev / station not configured yet): the JSON seed at
``backend/config/dive_deep_metrics.json``. The ``DIVE_DEEP_METRICS_FILE``
env var still overrides EVERYTHING (explicit file wins, workbook
included) for power users and tests.

Each metric entry is normalised to::

    {
        "key": "fdds",                       # unique id
        "label": "FDDS",                     # shown on the chip / row
        "metricName": "or_1st_day_delv_success",  # LPH technical name
        "format": "pct",                     # pct|dpmo|count|bps|rate
        "dir": "higher",                     # higher|lower
        "unit": "",                          # optional short suffix
        "category": "Delivery Performance",  # group header
    }

``load_dive_deep_catalog`` never raises: on any problem it falls back to
an empty list so the endpoint degrades gracefully.

JSON-fallback persistence in the frozen exe: the module tree lives inside
PyInstaller's ``_MEIPASS`` (a temp dir wiped on exit), so writing edits
next to the module would silently lose them on every restart. When the
launcher exports ``TOOLBOX_RUNTIME_STATE_DIR`` (``%LOCALAPPDATA%\\Toolbox``),
``catalog_path`` redirects to a per-user copy under ``<state>/config/``,
seeded from the embedded default on first use. This only matters while no
config workbook is available — with one, the workbook wins.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VALID_FORMATS = {"pct", "dpmo", "count", "bps", "rate"}
_VALID_DIRS = {"higher", "lower"}

# Config-workbook store (hidden KEY/VALUE sheet, same pattern as
# ``Tareas_Turnos``). VALUE is the ``{"groups": [...]}`` JSON document.
WORKBOOK_SHEET = "Dive_Deep"
WORKBOOK_KEY = "DIVE_DEEP_CATALOG"


def _env_override_path() -> Path | None:
    """Explicit ``DIVE_DEEP_METRICS_FILE`` override (wins over everything)."""
    override = os.environ.get("DIVE_DEEP_METRICS_FILE", "").strip().strip('"').strip("'")
    if override:
        candidate = Path(override)
        if candidate.exists():
            return candidate
    return None


def _workbook_catalog_data() -> dict[str, Any] | None:
    """Parsed catalog JSON stored in the config workbook, or ``None``.

    Reads through the cached KEY/VALUE reader (no re-parse per request).
    Any problem (no workbook, sheet/key missing, bad JSON) yields ``None``
    so callers fall back to the JSON file.
    """
    try:
        from app.core.tool_destinations import _read_keyvalue_rows

        for key_cell, value_cell in _read_keyvalue_rows(WORKBOOK_SHEET):
            if str(key_cell or "").strip().upper() != WORKBOOK_KEY:
                continue
            raw = str(value_cell or "").strip()
            if not raw:
                return None
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — workbook unreadable → JSON fallback
        return None
    return None


def _seed_path() -> Path:
    """The read-only default catalog shipped with the code/exe."""
    # backend/config/dive_deep_metrics.json (``_MEIPASS/backend/config``
    # when frozen — the spec bundles the seed there).
    here = Path(__file__).resolve().parents[2]
    return here / "config" / "dive_deep_metrics.json"


def catalog_path() -> Path:
    """Resolve the metrics JSON file (the NON-workbook store).

    Priority:
      1. ``DIVE_DEEP_METRICS_FILE`` env override (explicit wins).
      2. Launcher-managed runs (``TOOLBOX_RUNTIME_STATE_DIR`` set, i.e.
         the frozen exe): a per-user WRITABLE copy under
         ``%LOCALAPPDATA%\\Toolbox\\config\\``, seeded from the embedded
         default on first use so operator edits survive restarts
         (writes into ``_MEIPASS`` are wiped when the exe exits).
      3. Dev: the repo file at ``backend/config/dive_deep_metrics.json``.

    Note this is only the seed/fallback: when the station's config
    workbook carries a ``DIVE_DEEP_CATALOG`` value, that wins (see
    :func:`load_dive_deep_catalog` / :func:`save_dive_deep_catalog`).
    """
    override = _env_override_path()
    if override is not None:
        return override
    seed = _seed_path()
    state_dir = os.environ.get("TOOLBOX_RUNTIME_STATE_DIR", "").strip()
    if state_dir:
        user_copy = Path(state_dir) / "config" / "dive_deep_metrics.json"
        try:
            if not user_copy.exists():
                user_copy.parent.mkdir(parents=True, exist_ok=True)
                if seed.exists():
                    shutil.copyfile(seed, user_copy)
            return user_copy
        except OSError as exc:
            # Fall back to the read-only seed: loads keep working, only
            # persistence degrades (and the log says why).
            logger.warning("dive-deep catalog: user copy unavailable (%s): %s", user_copy, exc)
    return seed


def _normalise_metric(raw: dict[str, Any], category: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip()
    # Accept both ``metric_name`` (config style) and ``metricName`` (JS style).
    metric_name = str(raw.get("metric_name") or raw.get("metricName") or "").strip()
    label = str(raw.get("label") or key).strip()
    if not key or not metric_name:
        return None
    fmt = str(raw.get("format") or "pct").strip().lower()
    if fmt not in _VALID_FORMATS:
        fmt = "pct"
    direction = str(raw.get("dir") or "higher").strip().lower()
    if direction not in _VALID_DIRS:
        direction = "higher"
    return {
        "key": key,
        "label": label,
        "metricName": metric_name,
        "format": fmt,
        "dir": direction,
        "unit": str(raw.get("unit") or "").strip(),
        "featured": bool(raw.get("featured")),
        "category": category,
    }


def load_dive_deep_catalog() -> list[dict[str, Any]]:
    """Return the grouped metric catalog.

    Shape: ``[{"category": str, "metrics": [<normalised metric>, ...]}, ...]``.
    Duplicate keys are dropped (first wins). Returns ``[]`` on any error.

    Source priority: env-override file > config workbook > JSON seed.
    A workbook value that parses to no valid groups falls through to the
    JSON seed (saves validate non-empty, so that only happens on manual
    workbook edits gone wrong).
    """
    if _env_override_path() is None:
        data = _workbook_catalog_data()
        if data is not None:
            groups = _parse_groups(data)
            if groups:
                return groups
    path = catalog_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return _parse_groups(data)


def catalog_source_path() -> str:
    """Where the ACTIVE catalog actually lives (workbook or JSON file)."""
    if _env_override_path() is None and _workbook_catalog_data() is not None:
        try:
            from app.core.tool_destinations import _config_workbook_path

            path = _config_workbook_path()
            if path is not None:
                return str(path)
        except Exception:  # noqa: BLE001
            pass
    return str(catalog_path())


def _parse_groups(data: Any) -> list[dict[str, Any]]:
    """Normalise a ``{"groups": [...]}`` document into the load shape."""
    groups_raw = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups_raw, list):
        return []

    groups: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for group in groups_raw:
        if not isinstance(group, dict):
            continue
        category = str(group.get("category") or "").strip() or "Otras"
        metrics_raw = group.get("metrics")
        if not isinstance(metrics_raw, list):
            continue
        metrics: list[dict[str, Any]] = []
        for entry in metrics_raw:
            metric = _normalise_metric(entry, category)
            if metric is None or metric["key"] in seen_keys:
                continue
            seen_keys.add(metric["key"])
            metrics.append(metric)
        if metrics:
            groups.append({"category": category, "metrics": metrics})
    return groups


_DEFAULT_COMMENT = (
    "Catalogo de metricas del Dive Deep Tool. El operador puede "
    "anadir mas metricas desde Configuracion > Dive Deep (o editando este "
    "archivo). Cada metrica necesita: key, label, metric_name (nombre "
    "tecnico EXACTO de la API agregada de PerfectMile/LPH), format y dir. "
    "'category' agrupa las metricas. Override con DIVE_DEEP_METRICS_FILE."
)
_DEFAULT_FORMATS = (
    "format: pct | dpmo | count | bps | rate. dir: higher | lower. "
    "unit: opcional, texto corto junto al valor."
)


def save_dive_deep_catalog(groups: list[dict[str, Any]]) -> tuple[bool, str]:
    """Persist the grouped catalog.

    Destination: the station's config workbook (key ``DIVE_DEEP_CATALOG``
    on sheet ``Dive_Deep`` — atomic save + SharePoint sync-up, shared by
    the whole station). Falls back to the JSON file only when no workbook
    is available (dev / setup phase) or ``DIVE_DEEP_METRICS_FILE`` is set.

    ``groups`` mirrors the load shape. Each metric is re-validated and
    normalised before writing. Returns ``(ok, message)``; never raises.
    On failure ``message`` is a machine code (``invalid_format``,
    ``metric_fields``, ``duplicate_key:<key>``, ``empty``,
    ``workbook_locked`` or ``write_failed``) that the route maps to
    localized copy; on success it is the destination path.
    """
    if not isinstance(groups, list):
        return False, "invalid_format"

    clean_groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        category = str(group.get("category") or "").strip() or "Otras"
        metrics_raw = group.get("metrics")
        if not isinstance(metrics_raw, list):
            continue
        metrics: list[dict[str, Any]] = []
        for entry in metrics_raw:
            metric = _normalise_metric(entry, category)
            if metric is None:
                return False, "metric_fields"
            if metric["key"] in seen:
                return False, f"duplicate_key:{metric['key']}"
            seen.add(metric["key"])
            out = {
                "key": metric["key"],
                "label": metric["label"],
                "metric_name": metric["metricName"],
                "format": metric["format"],
                "dir": metric["dir"],
            }
            if metric["unit"]:
                out["unit"] = metric["unit"]
            if metric.get("featured"):
                out["featured"] = True
            metrics.append(out)
        if metrics:
            clean_groups.append({"category": category, "metrics": metrics})

    if not clean_groups:
        return False, "empty"

    # Preferred destination: the station's config workbook (skipped when
    # an explicit DIVE_DEEP_METRICS_FILE override is in force). Writes go
    # through the same atomic upsert + SharePoint sync-up as Tareas_Turnos.
    if _env_override_path() is None:
        wb_path = None
        try:
            from app.core.tool_destinations import _writable_workbook_path

            wb_path = _writable_workbook_path()
        except Exception:  # noqa: BLE001 — resolver unavailable → JSON fallback
            wb_path = None
        if wb_path is not None and Path(wb_path).exists():
            payload = json.dumps({"groups": clean_groups}, ensure_ascii=False)
            try:
                from app.core.watchdog import watchdog_pause
                from app.shared_excel.config_workbook import upsert_keyvalue_sheet

                with watchdog_pause():
                    upsert_keyvalue_sheet(wb_path, WORKBOOK_SHEET, {WORKBOOK_KEY: payload})
            except PermissionError:
                # Workbook open in Excel: do NOT silently divert to the JSON
                # file (the stale workbook value would win on the next load).
                return False, "workbook_locked"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dive-deep catalog workbook write failed (%s): %s", wb_path, exc
                )
                return False, "write_failed"
            return True, str(wb_path)

    # Fallback store: the JSON file (dev / no workbook configured yet).
    # Preserve the existing header comments when present.
    path = catalog_path()
    comment = _DEFAULT_COMMENT
    formats = _DEFAULT_FORMATS
    try:
        with path.open("r", encoding="utf-8") as handle:
            existing = json.load(handle)
        if isinstance(existing, dict):
            comment = existing.get("_comment") or comment
            formats = existing.get("_formats") or formats
    except (OSError, ValueError):
        pass

    document = {"_comment": comment, "_formats": formats, "groups": clean_groups}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        # Filesystem detail (paths, errno) stays in the server log only.
        logger.warning("dive-deep catalog write failed (%s): %s", path, exc)
        return False, "write_failed"
    return True, str(path)


def _prettify_label(metric_name: str) -> str:
    """Best-effort human label from a technical metric name."""
    text = metric_name.replace("_", " ").replace("-", " ").strip()
    # Drop common trailing qualifiers so the label stays short.
    for noise in (" amzl", " amzlonly", " only amzl", " perc2", " perc", " v2", " dsp3", " dsp"):
        if text.lower().endswith(noise):
            text = text[: -len(noise)]
    words = [w for w in text.split() if w]
    out: list[str] = []
    for w in words:
        upper = w.upper()
        if upper in {"DPMO", "RTS", "PNOV", "FDDS", "FTDS", "DEA", "DNR", "DMG", "OTD", "ONR", "BPS", "LU", "NOV", "EU", "NA"}:
            out.append(upper)
        else:
            out.append(w.capitalize())
    label = " ".join(out).strip()
    return label or metric_name


def infer_metric_metadata(
    metric_name: str,
    *,
    existing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Heuristic inference of label/category/format/dir/unit from a name.

    Used as the fallback when the AI is unavailable, and as the schema
    the AI is asked to mimic. ``existing`` is accepted for symmetry but
    the heuristic only needs the name itself.
    """
    name = str(metric_name or "").strip()
    low = name.lower()

    fmt = "pct"
    direction = "higher"
    unit = ""
    if "dpmo" in low:
        fmt, direction = "dpmo", "lower"
    elif "bps" in low:
        fmt, direction, unit = "bps", "lower", "bps"
    elif any(tok in low for tok in ("shipments", "units", "count", "volume", "ageing", "aging", "scrub", "lost", "missing")) or low.startswith("or_nov") or low.startswith("or_rts"):
        fmt, direction = "count", "lower"
    elif any(tok in low for tok in ("perc", "_per", "rate", "compliance", "success", "fdds", "ftds", "otd", "dpph", "fpy")):
        fmt = "pct"
        direction = "lower" if any(tok in low for tok in ("late", "miss", "fail", "reject", "robl")) else "higher"

    if any(tok in low for tok in ("dpmo", "conc", "dea", "concession", "affinity", "waterfall", "tenure")):
        category = "Concessions / Calidad (DPMO)"
    elif any(tok in low for tok in ("rts", "nov", "lost", "return", "scrub", "ageing", "aging", "missing")):
        category = "Returns / NOV"
    elif any(tok in low for tok in ("sort", "fpy", "stow", "instation", "induct", "sidelined")):
        category = "In-Station / Sort"
    else:
        category = "Delivery Performance"

    return {
        "label": _prettify_label(name),
        "category": category,
        "format": fmt,
        "dir": direction,
        "unit": unit,
    }
