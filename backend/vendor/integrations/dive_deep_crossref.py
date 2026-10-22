"""Row-level metric catalog + normaliser for Dive Deep "Cross IDs".

The aggregate Dive Deep grid (see ``dive_deep_metrics.py``) shows one
DPMO/percentage number per metric and window. This module powers the
complementary *cross-reference* view: it fetches the underlying
shipment ROWS for a metric (via the bulk PerfectMile client) and
normalises every row to ``{tracking_id, dsp, extra}`` so the SPA can:

* list tracking ids grouped by DSP, and
* intersect tracking ids across several selected metrics (e.g. which
  parcels are both LOST and a Concession ONR).

The catalog lives in ``backend/config/dive_deep_crossref_metrics.json``
(override with ``DIVE_DEEP_CROSSREF_FILE``) so operators add metrics
without touching code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

# Candidate column names, most-specific first. Different PerfectMile
# metrics expose the tracking id / DSP under different keys, so we probe
# a known list (mirrors the field detection already used by the FTDS /
# RTS / Cierre parsers).
_TRACKING_FIELDS = (
    "scannable_id",
    "tracking_id",
    "trackingId",
    "Tracking ID",
    "fulfillment_shipment_id",
    "shipment_id",
)
_DSP_FIELDS = (
    "provider_company_short_code",
    "provider_company_sc",
    "dsp_short_code",
    "latestDSPName",
    "compDAData.latestDSPName",
    "dsp",
    "short_code",
    "provider",
    "transporter",
    "DSP",
)


def catalog_path() -> Path:
    """Resolve the crossref metrics JSON file (env override wins)."""
    override = os.environ.get("DIVE_DEEP_CROSSREF_FILE", "").strip().strip('"').strip("'")
    if override:
        candidate = Path(override)
        if candidate.exists():
            return candidate
    here = Path(__file__).resolve().parents[2]
    return here / "config" / "dive_deep_crossref_metrics.json"


def load_crossref_catalog() -> list[dict[str, Any]]:
    """Return the grouped row-level metric catalog (``[]`` on error)."""
    try:
        with catalog_path().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    metrics_raw = data.get("metrics") if isinstance(data, dict) else None
    if not isinstance(metrics_raw, list):
        return []

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    seen: set[str] = set()
    for raw in metrics_raw:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        metric_key = str(raw.get("metric_key") or "").strip()
        metric_name = str(raw.get("metric_name") or "").strip()
        if not key or key in seen or (not metric_key and not metric_name):
            continue
        seen.add(key)
        category = str(raw.get("category") or "").strip() or "Otras"
        entry = {
            "key": key,
            "label": str(raw.get("label") or key).strip(),
            "metric_key": metric_key,
            "metric_name": metric_name,
            "category": category,
            "tracking_fields": _as_list(raw.get("tracking_fields")),
            "dsp_fields": _as_list(raw.get("dsp_fields")),
            "extra_fields": _as_list(raw.get("extra_fields")),
        }
        if category not in groups:
            groups[category] = {"category": category, "metrics": []}
            order.append(category)
        groups[category]["metrics"].append(entry)
    return [groups[c] for c in order]


def find_metric(key: str) -> dict[str, Any] | None:
    """Look up a single crossref metric definition by key."""
    for group in load_crossref_catalog():
        for metric in group["metrics"]:
            if metric["key"] == key:
                return metric
    return None


def _as_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return []


def _first_field(row: dict[str, Any], candidates: Iterable[str]) -> str:
    for field in candidates:
        if field in row:
            val = row.get(field)
            if val not in (None, ""):
                return str(val).strip()
    return ""


def normalise_rows(
    rows: Iterable[dict[str, Any]],
    *,
    tracking_fields: Iterable[str] | None = None,
    dsp_fields: Iterable[str] | None = None,
    extra_fields: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Normalise raw PerfectMile rows to ``{tracking_id, dsp, extra}``.

    ``tracking_fields`` / ``dsp_fields`` extend (take priority over) the
    built-in candidate lists. ``extra_fields`` are copied verbatim into
    an ``extra`` dict so the UI can show "relevant info" columns.
    """
    track_probe = list(tracking_fields or []) + list(_TRACKING_FIELDS)
    dsp_probe = list(dsp_fields or []) + list(_DSP_FIELDS)
    extras = list(extra_fields or [])

    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = _first_field(row, track_probe)
        if not tid:
            continue
        dsp = _first_field(row, dsp_probe) or "—"
        record: dict[str, Any] = {"tracking_id": tid, "dsp": dsp.upper()}
        if extras:
            record["extra"] = {f: row.get(f) for f in extras if f in row}
        out.append(record)
    return out
