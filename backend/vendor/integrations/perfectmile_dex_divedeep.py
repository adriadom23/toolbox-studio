"""Backward-compatible shim for the former DEX-only dive-deep bridge.

The transport primitives were generalized into the shared, metric-agnostic
``app.integrations.perfectmile_caravan_export`` layer so every row-level
consumer can reuse the same invisible Caravan-issued export acquisition. This
module keeps the DEX transport aliases used by ``dex.py``.

Nothing here constructs a legacy host/``bulk.csv``/``bulk.json`` URL; every URL
still comes only from Caravan ``getDiveDeepUrl`` via the shared layer. See that
module's banner for the removal path and the Andes migration target.
"""
from __future__ import annotations

from typing import Any

from app.integrations.perfectmile_caravan_client import CaravanAuthError
from app.integrations.perfectmile_caravan_export import (
    CaravanExportAuthError,
    CaravanExportError,
    CaravanExportUnavailable,
    fetch_export_rows,
    get_export_url,
    parse_csv_bytes,
    _parse_csv_text,
    _MAX_CSV_BYTES,
)

# Arm -> Caravan/LPH metric name. These are the two DEX feedback metrics.
DEX_DIVEDEEP_METRICS: dict[str, str] = {
    "never": "cust_feedb_neg_never_received_delivery",
    "wrong": "cust_feedb_neg_delivered_to_wrong_address",
}

# Backward-compatible error aliases (same class objects as the shared layer so
# ``except DexDiveDeepAuthError`` and ``except CaravanExportAuthError`` catch the
# same exceptions).
DexDiveDeepError = CaravanExportError
DexDiveDeepAuthError = CaravanExportAuthError
DexDiveDeepUnavailable = CaravanExportUnavailable


def get_divedeep_url(
    metric_name: str,
    station: str,
    date: str,
    *,
    region: str = "EU",
) -> str | None:
    """DEX-compatible wrapper around :func:`get_export_url` (single date)."""
    return get_export_url(metric_name, station, date, region=region)


def fetch_divedeep_rows(
    metric_name: str,
    station: str,
    date: str,
    *,
    region: str = "EU",
) -> list[dict[str, Any]]:
    """DEX-compatible wrapper around :func:`fetch_export_rows` (single date)."""
    return fetch_export_rows(metric_name, station, date, region=region)


__all__ = [
    "DEX_DIVEDEEP_METRICS",
    "DexDiveDeepAuthError",
    "DexDiveDeepError",
    "DexDiveDeepUnavailable",
    "CaravanAuthError",
    "fetch_divedeep_rows",
    "get_divedeep_url",
    "parse_csv_bytes",
    "_parse_csv_text",
    "_MAX_CSV_BYTES",
]
