"""Route Planning Plan Manifest client.

The legacy ``https://routingtools.amazon.com/downloadManifest`` servlet was
retired in August 2026. The current Route Planning SPA obtains manifests in
two steps:

1. ``GET /ws/api/downloadManifest`` with ``stationCode``, ``cycleName`` and
   ``planDate`` returns a short-lived presigned S3 URL as plain text.
2. The workbook is downloaded from that URL without forwarding session
   cookies.

Authentication uses the shared Last-Mile Planning handshake implemented by
:mod:`app.integrations.lastmile_planning` (Midway id token -> session-token).
The public classes and return shape in this module stay stable so the
``/api/manifest/fetch`` and ``/api/manifest/fetch-content`` consumers do not
need to change.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit

import requests

from app.integrations.lastmile_planning import (
    ROUTE_PLANNING_APP,
    PlanningAuthError,
    PlanningNetworkError,
    PlanningRequestError,
    get_json,
    get_text,
    list_supported_cycles,
    login_status,
    normalize_cycle_name,
    normalize_date,
    normalize_station,
)


ROUTINGTOOLS_BASE_URL = ROUTE_PLANNING_APP.api_base
ROUTINGTOOLS_DOWNLOAD_PATH = "/api/downloadManifest"
ROUTINGTOOLS_LOGIN_URL = ROUTE_PLANNING_APP.site + "/"
ROUTINGTOOLS_DEFAULT_TIMEOUT = 30.0

# Allowed ``cycleName`` values surfaced as a dropdown in the wizard.
ALLOWED_CYCLES: tuple[str, ...] = (
    "AD_HOC_1",
    "AD_HOC_2",
    "CRTN",
    "CYCLE_1",
    "CYCLE_2",
    "CYCLE_EMER",
    "CYCLE_SD_A",
    "CYCLE_SD_B",
    "CYCLE_SD_C",
    "CYCLE_SD_D",
    "High_Value",
    "REDELIVERY",
    "RTS_1",
    "RTS_2",
    "RTS_3",
    "SWA_AM",
    "SWA_EARLY_AM",
    "SWA_INJECTION_CYCLE",
    "SWA_LATE_PM",
    "SWA_PM",
    "TEST_1",
)
DEFAULT_CYCLE = "CYCLE_1"

# Both formats are produced in practice: the current endpoint returns legacy
# XLS/OLE2 while manually supplied or future manifests may be XLSX/ZIP.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK"
_MIN_VALID_WORKBOOK_BYTES = 512


class RoutingToolsAuthError(RuntimeError):
    """Raised when Route Planning rejects the request for missing auth."""


class RoutingToolsClientError(RuntimeError):
    """Raised when Route Planning returns an unexpected response."""


@dataclass
class ManifestDownload:
    content: bytes
    filename: str
    status_code: int
    content_type: str
    bytes_read: int

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "content_type": self.content_type,
            "bytes_read": self.bytes_read,
            "filename": self.filename,
        }


def _normalize_cycle(cycle: str | None) -> str:
    raw = (cycle or "").strip()
    if not raw:
        return DEFAULT_CYCLE
    upper = raw.upper().replace(" ", "_")
    for allowed in ALLOWED_CYCLES:
        if allowed.upper() == upper:
            return allowed
    raise ValueError(
        f"Cycle '{raw}' no esta en la lista permitida: {', '.join(ALLOWED_CYCLES)}"
    )


def _normalize_station_code(station_code: str) -> str:
    code = (station_code or "").strip().upper()
    if not code or not code.isalnum() or len(code) > 6:
        raise ValueError(f"Station code invalido: {station_code!r}")
    return code


def _normalize_plan_date(plan_date: date | datetime | str | None) -> str:
    if plan_date is None:
        return date.today().isoformat()
    if isinstance(plan_date, datetime):
        return plan_date.date().isoformat()
    if isinstance(plan_date, date):
        return plan_date.isoformat()
    text = str(plan_date).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"plan_date no parseable: {plan_date!r}")


def cookies_status() -> dict[str, Any]:
    """Report whether Route Planning can mint a usable session-token."""
    info = login_status(ROUTE_PLANNING_APP)
    ready = bool(info.get("ready"))
    return {
        **info,
        "ready": ready,
        "missing": [] if ready else ["session-token"],
        "cookie_count": 1 if ready else 0,
        "login_url": ROUTINGTOOLS_LOGIN_URL,
    }


def _validate_presigned_url(value: str) -> str:
    url = (value or "").strip().strip('"')
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not host.endswith(".amazonaws.com")
        or ".s3." not in host
    ):
        raise RoutingToolsClientError(
            "Route Planning no devolvio una URL S3 de descarga valida."
        )
    return url


def _validate_workbook(content: bytes) -> None:
    valid_magic = content.startswith(_OLE2_MAGIC) or content.startswith(_ZIP_MAGIC)
    if len(content) < _MIN_VALID_WORKBOOK_BYTES or not valid_magic:
        raise RoutingToolsClientError(
            f"Route Planning devolvio un manifest invalido ({len(content)} bytes)."
        )


def download_plan_manifest(
    *,
    station_code: str,
    cycle: str | None = None,
    plan_date: date | datetime | str | None = None,
    timeout: float = ROUTINGTOOLS_DEFAULT_TIMEOUT,
    cookies: dict[str, str] | None = None,
) -> ManifestDownload:
    """Fetch the Plan Manifest workbook for one station/cycle/date.

    ``cookies`` is retained for source compatibility with the legacy client;
    Route Planning authentication is now handled centrally through the
    session-token handshake.
    """
    code = _normalize_station_code(station_code)
    cycle_norm = _normalize_cycle(cycle)
    plan_iso = _normalize_plan_date(plan_date)
    del cookies

    query = urlencode(
        {
            "stationCode": code,
            "cycleName": cycle_norm,
            "planDate": plan_iso,
        }
    )
    return_path = f"/route-planning/{code}/"

    try:
        presigned = get_text(
            ROUTE_PLANNING_APP,
            f"{ROUTINGTOOLS_DOWNLOAD_PATH}?{query}",
            return_path=return_path,
            timeout=timeout,
        )
    except PlanningAuthError as exc:
        raise RoutingToolsAuthError(str(exc)) from exc
    except (PlanningNetworkError, PlanningRequestError) as exc:
        raise RoutingToolsClientError(str(exc)) from exc

    download_url = _validate_presigned_url(presigned)
    try:
        response = requests.get(download_url, timeout=max(timeout, 60.0))
    except requests.exceptions.Timeout as exc:
        raise RoutingToolsClientError(
            "La descarga del Plan Manifest no respondio a tiempo."
        ) from exc
    except requests.exceptions.SSLError as exc:
        raise RoutingToolsClientError(
            f"Error SSL descargando el Plan Manifest: {exc}"
        ) from exc
    except requests.RequestException as exc:
        raise RoutingToolsClientError(
            f"Error de red descargando el Plan Manifest: {exc}"
        ) from exc

    if response.status_code in (401, 403):
        raise RoutingToolsAuthError(
            f"La URL temporal del Plan Manifest fue rechazada ({response.status_code})."
        )
    if response.status_code != 200:
        raise RoutingToolsClientError(
            f"La descarga del Plan Manifest devolvio HTTP {response.status_code}."
        )

    body = response.content or b""
    _validate_workbook(body)

    filename = Path(urlsplit(download_url).path).name
    if not filename.lower().endswith((".xls", ".xlsx", ".xlsm")):
        filename = f"{code}_{cycle_norm}_{plan_iso}.xls"

    content_type = (
        response.headers.get("Content-Type")
        or "application/vnd.ms-excel"
    ).lower()
    return ManifestDownload(
        content=body,
        filename=filename,
        status_code=response.status_code,
        content_type=content_type,
        bytes_read=len(body),
    )


def fetch_route_plan_ids(
    *,
    station_code: str,
    cycles: Iterable[str],
    plan_date: date | datetime | str | None = None,
    timeout: float = ROUTINGTOOLS_DEFAULT_TIMEOUT,
) -> dict[str, str]:
    """Return the active Route Planning ``{route_code: planId}`` mapping.

    ``routePlanSummaries[].planId`` is the canonical daily plan id. Each
    active plan's official Routes export supplies the exact route codes, so
    plans from different cycles/clusters are associated per route without
    deriving either identifier from display text.
    """
    code = normalize_station(station_code)
    date_iso = normalize_date(plan_date)
    wanted = {normalize_cycle_name(cycle) for cycle in cycles if normalize_cycle_name(cycle)}
    supported = {
        normalize_cycle_name(str(item.get("cycleName") or "")): str(item.get("cycleId") or "").strip()
        for item in list_supported_cycles(ROUTE_PLANNING_APP, code, date_iso, timeout=timeout)
        if item.get("cycleId")
    }
    if not wanted:
        wanted = set(supported)
    route_to_plan: dict[str, str] = {}
    summaries: list[tuple[int, str]] = []
    for cycle in wanted:
        cycle_id = supported.get(cycle)
        if not cycle_id:
            continue
        return_path = f"/route-planning/{code}/{cycle_id}/{date_iso}/"
        states = get_json(
            ROUTE_PLANNING_APP,
            f"/api/routePlans/{code}/{cycle_id}?planDate={date_iso}&latestOnly=false",
            return_path=return_path,
            timeout=timeout,
        )
        for cluster in states if isinstance(states, list) else ():
            for summary in cluster.get("routePlanSummaries") or ():
                plan_id = str(summary.get("planId") or "").strip()
                if plan_id and summary.get("isActivePlan"):
                    summaries.append((int(summary.get("createdDateTime") or 0), plan_id))

    for _created, plan_id in sorted(set(summaries)):
        presigned = get_text(
            ROUTE_PLANNING_APP,
            f"/api/routePlan/export/{plan_id}/routes?stationCode={code}",
            return_path=f"/route-planning/{code}/",
            timeout=timeout,
        )
        response = requests.get(_validate_presigned_url(presigned), timeout=max(timeout, 60.0))
        if response.status_code != 200:
            raise RoutingToolsClientError(
                f"Route Planning routes export devolvio HTTP {response.status_code}."
            )
        for row in csv.DictReader(io.StringIO((response.text or "").lstrip("\ufeff"))):
            route_code = str(row.get("Route Code") or "").strip().upper()
            if route_code:
                route_to_plan[route_code] = plan_id
    return route_to_plan


__all__ = [
    "ALLOWED_CYCLES",
    "DEFAULT_CYCLE",
    "ManifestDownload",
    "ROUTINGTOOLS_BASE_URL",
    "ROUTINGTOOLS_DOWNLOAD_PATH",
    "ROUTINGTOOLS_LOGIN_URL",
    "RoutingToolsAuthError",
    "RoutingToolsClientError",
    "cookies_status",
    "download_plan_manifest",
    "fetch_route_plan_ids",
]
