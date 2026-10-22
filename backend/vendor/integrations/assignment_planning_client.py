"""DA Assignment (`eu.assignment.planning.last-mile.a2z.com`) client.

Thin wrapper over :mod:`app.integrations.lastmile_planning` that returns
the per-route DSP/DA assignment for a station + cycle + date.

The SPA builds its CSV (``Route, Service Type, ..., DSP, DA, DA_ID,
Suggested DSP, Suggested DA``) from three endpoints:

* ``/web-service/api/routeSummaries/{station}/{cycleId}/{date}`` — the
  per-route records (``routeCode``, ``dspId`` company UUID, ``daId``,
  ``recommendedAssignment``, ``serviceTypeId``, ``packageCount`` ...).
* ``/web-service/api/providers?stationCode={station}`` — maps the DSP
  company UUID (``companyId``) to its short code (``DESL``, ``JSIS`` ...).
* ``/web-service/api/serviceTypes`` — maps ``serviceTypeId`` to a name.

We reproduce that join server-side and return a normalised list the
Rutas HUB frontend can consume directly (it only needs Route + DSP, but
we expose the rest too).

Public surface:

* :func:`assignment_login_status`
* :func:`fetch_assignment`
* error classes ``AssignmentPlanning*`` (aliases of the shared ones).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.integrations.lastmile_planning import (
    ASSIGNMENT_APP,
    PlanningAuthError as AssignmentPlanningAuthError,
    PlanningError as AssignmentPlanningError,
    PlanningNetworkError as AssignmentPlanningNetworkError,
    PlanningRequestError as AssignmentPlanningRequestError,
    get_json,
    login_status,
    normalize_date,
    normalize_station,
    resolve_cycle_id,
)


ASSIGNMENT_SITE = ASSIGNMENT_APP.site
DEFAULT_CYCLE = "CYCLE_1"


def assignment_login_status() -> dict[str, Any]:
    """Report whether DA Assignment can be auto-fetched (Midway session)."""
    return login_status(ASSIGNMENT_APP)


def _provider_shortcode_map(station: str, *, timeout: float | None) -> dict[str, str]:
    """Map provider company UUID -> short code (e.g. ``DESL``)."""
    kwargs = {"timeout": timeout} if timeout else {}
    data = get_json(
        ASSIGNMENT_APP,
        f"/api/providers?stationCode={station}",
        return_path=f"/{station}",
        **kwargs,
    )
    out: dict[str, str] = {}
    if isinstance(data, list):
        for prov in data:
            if not isinstance(prov, dict):
                continue
            short = str(prov.get("companyShortCode") or "").strip()
            # routeSummaries.dspId matches providers.companyId.
            for key in ("companyId", "providerId"):
                val = str(prov.get(key) or "").strip()
                if val and short:
                    out[val] = short
    return out


def _service_type_map(*, timeout: float | None) -> dict[str, str]:
    """Map serviceTypeId -> human service type name."""
    kwargs = {"timeout": timeout} if timeout else {}
    try:
        data = get_json(ASSIGNMENT_APP, "/api/serviceTypes", return_path="/", **kwargs)
    except AssignmentPlanningError:
        return {}
    out: dict[str, str] = {}
    if isinstance(data, list):
        for st in data:
            if isinstance(st, dict):
                sid = str(st.get("serviceTypeId") or "").strip()
                name = str(st.get("serviceTypeName") or "").strip()
                if sid and name:
                    out[sid] = name
    return out


def fetch_assignment(
    *,
    station: str,
    cycle: str = DEFAULT_CYCLE,
    plan_date: date | datetime | str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return the per-route DA Assignment for ``station`` / ``cycle`` / ``plan_date``.

    Response shape::

        {
          "routes": [
            {"route": "CA_A70", "dsp": "JSIS", "da": "A14F2BY82N2NG2",
             "daId": "A14F2BY82N2NG2", "suggestedDa": "A1XJIVWKAF6PRO",
             "serviceType": "Standard Parcel", "packages": 98,
             "status": "ASSIGNED"},
            ...
          ]
        }

    Route + DSP are what the Rutas HUB ``parseAssignmentCSV`` needs; the
    rest is exposed for richer tools.
    """
    code = normalize_station(station)
    date_iso = normalize_date(plan_date)
    kwargs = {"timeout": timeout} if timeout else {}

    cycle_id = resolve_cycle_id(ASSIGNMENT_APP, code, cycle or DEFAULT_CYCLE, date_iso, **kwargs)

    return_path = f"/assignment-planning/{code}/{cycle_id}/{date_iso}"
    summaries = get_json(
        ASSIGNMENT_APP,
        f"/api/routeSummaries/{code}/{cycle_id}/{date_iso}",
        return_path=return_path,
        **kwargs,
    )
    records = []
    if isinstance(summaries, dict):
        records = summaries.get("routeSummaries") or []
    elif isinstance(summaries, list):
        records = summaries
    if not isinstance(records, list):
        records = []

    provider_map = _provider_shortcode_map(code, timeout=timeout)
    service_map = _service_type_map(timeout=timeout)

    routes: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        route_code = str(rec.get("routeCode") or "").strip()
        if not route_code:
            continue
        dsp_uuid = str(rec.get("dspId") or "").strip()
        dsp = provider_map.get(dsp_uuid, dsp_uuid)
        service_id = str(rec.get("serviceTypeId") or "").strip()
        routes.append(
            {
                "route": route_code,
                "dsp": dsp,
                "da": str(rec.get("daId") or "").strip(),
                "daId": str(rec.get("daId") or "").strip(),
                "suggestedDa": str(rec.get("recommendedAssignment") or "").strip(),
                "serviceType": service_map.get(service_id, ""),
                "packages": int(rec.get("packageCount") or 0),
                "status": str(rec.get("status") or "").strip(),
            }
        )

    return {"routes": routes, "station": code, "cycle": cycle, "cycleId": cycle_id, "date": date_iso}


__all__ = [
    "ASSIGNMENT_SITE",
    "DEFAULT_CYCLE",
    "AssignmentPlanningAuthError",
    "AssignmentPlanningError",
    "AssignmentPlanningNetworkError",
    "AssignmentPlanningRequestError",
    "assignment_login_status",
    "fetch_assignment",
]
