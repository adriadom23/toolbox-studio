"""Dispatch Planning (`eu.dispatch.planning.last-mile.a2z.com`) client.

Thin wrapper over :mod:`app.integrations.lastmile_planning` that fetches
the **Route Sheets** for a station + cycle + date. The cycle (CYCLE_1,
RTS_2, ...) is resolved to its ``cycleId`` UUID via the app's
``routingConfig`` endpoint, so the caller only passes the human cycle
selected in the Plan Manifest panel.

Public surface (kept stable for existing callers):

* :func:`route_sheets_login_status`
* :func:`fetch_route_sheets`
* error classes ``DispatchPlanning*`` (aliases of the shared ones).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests

from app.integrations.lastmile_planning import (
    DISPATCH_APP,
    PlanningAuthError as DispatchPlanningAuthError,
    PlanningError as DispatchPlanningError,
    PlanningNetworkError as DispatchPlanningNetworkError,
    PlanningRequestError as DispatchPlanningRequestError,
    get_json,
    get_text,
    login_status,
    normalize_date,
    normalize_station,
    resolve_cycle_id,
)


DISPATCH_SITE = DISPATCH_APP.site

# Default cycle when none is supplied (CYCLE_1 is the standard wave).
DEFAULT_CYCLE = "CYCLE_1"
# CYCLE_1's well-known cycleId, used only as a last-resort fallback when
# routingConfig cannot be reached. routingConfig is the source of truth.
DEFAULT_PROGRAM_ID = "761553f5-9fc1-4cef-8815-b974bc63f0a9"


def route_sheets_login_status() -> dict[str, Any]:
    """Report whether Route Sheets can be auto-fetched (Midway session)."""
    return login_status(DISPATCH_APP)


def day_of_ops_plan_login_status() -> dict[str, Any]:
    """Report whether the Day of Ops Plan can be auto-fetched (Midway session)."""
    return login_status(DISPATCH_APP)


@dataclass(frozen=True)
class DayOfOpsPlanDownload:
    """Result of a Day of Ops Plan download (the raw XLSX + metadata)."""

    content: bytes
    filename: str
    station: str
    cycle: str
    plan_date: str

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "station": self.station,
            "cycle": self.cycle,
            "plan_date": self.plan_date,
            "size": len(self.content),
        }


def fetch_day_of_ops_plan(
    *,
    station: str,
    cycle: str = DEFAULT_CYCLE,
    plan_date: date | datetime | str | None = None,
    timeout: float | None = None,
) -> DayOfOpsPlanDownload:
    """Download the **Day of Ops Plan** XLSX for ``station`` / ``cycle`` / ``plan_date``.

    Mirrors the SPA's "Day of Ops Plan" download button:

    1. Resolve the human ``cycle`` (CYCLE_1, ...) to its ``cycleId`` UUID
       via ``routingConfig``.
    2. ``GET /api/dispatchPlan/{station}/{cycleId}/{date}`` to read the
       finalized plan and its ``dspPlan.requestId`` (the plan version).
    3. ``GET /api/dayOfOpsPlan/{station}/{cycleId}/{date}/{requestId}``
       returns a short-lived presigned S3 URL (plain text).
    4. Download the XLSX from that presigned URL.

    Returns a :class:`DayOfOpsPlanDownload` with the raw workbook bytes.
    """
    code = normalize_station(station)
    date_iso = normalize_date(plan_date)
    timeout_kwargs = {"timeout": timeout} if timeout else {}

    cycle_id = resolve_cycle_id(DISPATCH_APP, code, cycle or DEFAULT_CYCLE, date_iso, **timeout_kwargs)

    return_path = f"/dispatch-planning/{code}/{cycle_id}/{date_iso}?laborType=DSP"

    plan = get_json(
        DISPATCH_APP,
        f"/api/dispatchPlan/{code}/{cycle_id}/{date_iso}",
        return_path=return_path,
        **timeout_kwargs,
    )
    if not isinstance(plan, dict):
        raise DispatchPlanningRequestError("Dispatch Planning devolvio un dispatchPlan inesperado.")
    dsp_plan = plan.get("dspPlan") or {}
    request_id = str(dsp_plan.get("requestId") or "").strip()
    if not request_id:
        raise DispatchPlanningRequestError(
            "No hay un plan finalizado para ese station/cycle/fecha (sin requestId)."
        )

    presigned_url = get_text(
        DISPATCH_APP,
        f"/api/dayOfOpsPlan/{code}/{cycle_id}/{date_iso}/{request_id}",
        return_path=return_path,
        **timeout_kwargs,
    )
    if not presigned_url.lower().startswith("http"):
        raise DispatchPlanningRequestError(
            "dayOfOpsPlan no devolvio una URL de descarga valida."
        )

    try:
        resp = requests.get(presigned_url, timeout=timeout or 60.0)
    except requests.exceptions.Timeout as exc:
        raise DispatchPlanningNetworkError(
            "La descarga del Day of Ops Plan no respondio a tiempo."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise DispatchPlanningNetworkError(
            f"No se pudo descargar el Day of Ops Plan: {exc}"
        ) from exc

    if resp.status_code != 200 or not resp.content:
        raise DispatchPlanningRequestError(
            f"La descarga del Day of Ops Plan fallo (HTTP {resp.status_code})."
        )

    cycle_label = (cycle or DEFAULT_CYCLE).strip().upper().replace(" ", "_")
    filename = f"{date_iso}-{code}-{cycle_label}-DSP-DayOfOpsPlan.xlsx"
    return DayOfOpsPlanDownload(
        content=resp.content,
        filename=filename,
        station=code,
        cycle=cycle_label,
        plan_date=date_iso,
    )


def fetch_route_sheets(
    *,
    station: str,
    cycle: str = DEFAULT_CYCLE,
    plan_date: date | datetime | str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return the raw route-sheets JSON for ``station`` / ``cycle`` / ``plan_date``.

    The payload mirrors the SPA's ``/ws/api/routeSheets`` response
    (``{"routeSheets": [...], "sortZoneIdToTaskType": {...}}``), which the
    Rutas HUB frontend already parses.

    ``cycle`` is the human label (``CYCLE_1``, ``RTS_2`` ...). It is
    resolved to its ``cycleId`` UUID via ``routingConfig``.
    """
    code = normalize_station(station)
    date_iso = normalize_date(plan_date)
    kwargs = {"timeout": timeout} if timeout else {}

    cycle_id = resolve_cycle_id(DISPATCH_APP, code, cycle or DEFAULT_CYCLE, date_iso, **kwargs)

    path = f"/api/routeSheets/{code}/{cycle_id}/{date_iso}"
    return_path = f"/route-sheets/{code}/{cycle_id}/{date_iso}?laborType=DSP"
    data = get_json(DISPATCH_APP, path, return_path=return_path, **kwargs)
    if not isinstance(data, dict):
        raise DispatchPlanningRequestError("Dispatch Planning devolvio un JSON con forma inesperada.")
    return data


__all__ = [
    "DEFAULT_CYCLE",
    "DEFAULT_PROGRAM_ID",
    "DISPATCH_SITE",
    "DayOfOpsPlanDownload",
    "DispatchPlanningAuthError",
    "DispatchPlanningError",
    "DispatchPlanningNetworkError",
    "DispatchPlanningRequestError",
    "day_of_ops_plan_login_status",
    "fetch_day_of_ops_plan",
    "fetch_route_sheets",
    "route_sheets_login_status",
]
