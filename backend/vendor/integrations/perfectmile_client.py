"""Centralised PerfectMile retrieval client.

This module replaces the Selenium/Firefox dance from
``legacy/original_scripts/PerfectmileRetireva.py`` with a stateless
HTTP client that:

1. Reads the user's Firefox cookies for ``perfectmile.amazon.com`` via
   :mod:`app.integrations.firefox_cookies`. If they are missing or
   expired we instruct the caller to open the PerfectMile login URL
   (same fallback pattern DINO uses).
2. Calls the ``blue_sky/dive_deep/bulk.json`` endpoint day by day with
   the same filters/payload shape as legacy (validated end-to-end).
3. Returns a typed :class:`PerfectMileResult` with the rows and
   diagnostics so any tool (DEX, DPR, Bridge, etc.) can reuse it by
   asking for a ``metric_name`` from :data:`METRIC_CATALOG` or by
   providing a custom one.

Designed so other scripts migrate to the same retrieval entry point.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import requests
import urllib3

from app.integrations.firefox_cookies import (
    PERFECTMILE_HOST_PATTERNS,
    perfectmile_cookies_from_profile,
)


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


PERFECTMILE_HOME_URL = "https://perfectmile.amazon.com/"
PERFECTMILE_BULK_URL = "https://perfectmile.amazon.com/blue_sky/dive_deep/bulk.json"
PERFECTMILE_DEFAULT_MAPPING = "perfectmile-node-mapping-prod"
PERFECTMILE_DEFAULT_COUNTRY = "ES"
PERFECTMILE_DEFAULT_LOCATION_TYPES = ("3P", "WW", "DS", "XP")
PERFECTMILE_DEFAULT_TIMEOUT_SEC = 120


CONCESSION_KEYS = (
    "order_id",
    "concession_id",
    "fulfillment_shipment_id",
    "tracking_id",
    "concession_creation_datetime",
    "concession_units",
    "concession_type_code",
    "concession_event_name",
    "issuer_agency_type",
    "source",
    "address_id",
    "country_code",
    "marketplace_id",
    "zip",
    "asin",
    "gross_concession_usd",
    "order_date",
    "ship_date",
    "attempted_delivery_datetime",
    "actual_delivery_datetime",
    "ship_method",
    "ship_option",
    "warehouse_id",
    "delivery_station_code",
    "concession_bucket_l1",
    "concession_bucket_l2",
    "concession_bucket_l3",
    "operations_ownership",
    "ownership_bucket",
    "clock_stop_event_datetime_utc",
    "external_estimated_arrival_date",
    "external_promised_delivery_date_item",
    "concession_type_code_group",
    "owner_type",
    "owner_node_id",
    "carrier",
    "shipment_id",
    "conceded_order_item_id",
    "conceded_shipment_item_id",
    "unit_id",
    "return_item_id",
    "lt_attempt_pro_company_name",
    "lt_attempt_pro_company_sc",
    "lt_dispatch_pro_company_name",
    "lt_dispatch_pro_company_sc",
    "lt_attempt_transporter_id",
    "lt_attempt_op_employee_id",
    "issuer",
    "lt_attempt_shipment_reason",
    "lt_attempt_shipment_status",
    "country",
)


DEX_FEEDBACK_KEYS = (
    "delivery_station_country_code",
    "delivery_station_code",
    "provider_company_short_code",
    "shipment_ship_option",
    "transporter_id",
    "choice_identifier",
    "tracking_id",
    "event_datetime",
    "country",
)


# Catalog shared across tools. Each entry describes how to fire the
# request for a logical metric. Tools can extend this at runtime by
# calling :func:`register_metric`.
METRIC_CATALOG: dict[str, dict[str, Any]] = {
    "concessions": {
        "metric_name": "conceded_units_only_amzl_wankel",
        "keys": CONCESSION_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Concessions bucket (DNR, DMG, LATE, ND, Other).",
    },
    "dnr": {
        "metric_name": "conceded_units_only_amzl_wankel",
        "keys": CONCESSION_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Concessions filtered down to DNR bucket.",
    },
    "dmg": {
        "metric_name": "conceded_units_only_amzl_wankel",
        "keys": CONCESSION_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Concessions filtered down to Damaged bucket.",
    },
    "late": {
        "metric_name": "conceded_units_only_amzl_wankel",
        "keys": CONCESSION_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Concessions filtered down to Late bucket.",
    },
    "nd": {
        "metric_name": "conceded_units_only_amzl_wankel",
        "keys": CONCESSION_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Concessions Not Delivered bucket.",
    },
    "dex_never_received": {
        "metric_name": "cust_feedb_neg_never_received_delivery",
        "keys": DEX_FEEDBACK_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "5.7 DEX - Negative - Never received delivery.",
    },
    "dex_wrong_address": {
        "metric_name": "cust_feedb_neg_delivered_to_wrong_address",
        "keys": DEX_FEEDBACK_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "5.4 DEX - Negative - Delivered to wrong address.",
    },
    "wubl": {
        "metric_name": "wubl60drivercount",
        "page_size": 5000,
        "pages_per_request": 1,
        "country_filter_key": "country",
        "description": "WUBL 60 driver count.",
    },
    "dwc": {
        "metric_name": "dwc20_misses",
        "page_size": 5000,
        "pages_per_request": 20,
        "country_filter_key": "country",
        "description": "DWC 20 misses.",
    },
    "wobl30": {
        "metric_name": "wobl30drivercount",
        "page_size": 5000,
        "pages_per_request": 1,
        "country_filter_key": "country",
        "description": "WOBL 30 driver count.",
    },
    "wobl60driverper": {
        # WOBL 60 driver percentage — flags drivers/routes that wrapped up
        # before last with 60+ minutes margin. Used by the RTS x DA dive
        # deep to tag returns whose DA finished early.
        "metric_name": "wobl60driverper",
        "page_size": 5000,
        "pages_per_request": 1,
        "country_filter_key": "country",
        "description": "WOBL 60 driver percentage (early finishers).",
    },
    "robl30": {
        "metric_name": "robl30",
        "page_size": 5000,
        "pages_per_request": 1,
        "country_filter_key": "country",
        "description": "ROBL30.",
    },
    "pnov": {
        "metric_name": "or_nov_shipments_v2",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "PNOV shipments.",
    },
    "pnov_adjusted": {
        # Adjusted PNOV (Wankel) used by the Handover PM tool. Returns one
        # row per PNOV shipment, so the row count is the PNOV count.
        "metric_name": "adjusted_pnov_wankel",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Adjusted PNOV (adjusted_pnov_wankel) used by Handover.",
    },
    "lost": {
        "metric_name": "ssh_lost_shipments",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Lost shipments.",
    },
    "scrub_lost": {
        # "EOD Scrub : Lost" review metric. metric_name as exposed by the
        # PerfectMile bulk endpoint (one row per scrub-lost shipment).
        "metric_name": "scrublost",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "EOD Scrub : Lost shipments.",
    },
    "scrub_missing": {
        # "EOD Scrub : Missing" review metric.
        "metric_name": "scrubmissing",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "EOD Scrub : Missing shipments.",
    },
    "rts_returns": {
        "metric_name": "or_rts_amzl_nd",
        # Do NOT restrict ``keys`` here. ``or_rts_amzl_nd`` exposes its
        # own column set (address_type, destination_address_id,
        # event_datetime, event_source, scannable_id, ...). When we
        # pass an explicit ``keys`` filter that intersects poorly with
        # those, PerfectMile silently drops the columns we count on
        # (e.g. tracking ID), and the FTDS parser ends up with zero
        # matchable rows. Letting PerfectMile decide returns the full
        # native schema for the metric.
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "RTS returns (or_rts_amzl_nd) used by FTDS/RTS and DEX.",
    },
    "rts_shipments": {
        "metric_name": "or_rts_amzl_nd",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "RTS shipments (or_rts_amzl_nd) filtered per station, used by Cierre.",
    },
    "fdds_ct_failure": {
        # Central Team owned Failures (DCR side of the Scorecard
        # Exceptions tool) — one row per failed shipment whose root
        # cause is owned by Central Team (routing, RGU, ...). The
        # native schema carries the DSP, route, tracking id and the
        # "sub bucket" used as the failure reason. No ``keys``
        # restriction on purpose so PerfectMile returns the full
        # native column set (same gotcha as ``rts_returns``).
        "metric_name": "fdds_ct_failure",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Central Team owned failures (fdds_ct_failure) used by Scorecard Exceptions.",
    },
    "lor_dpmo": {
        # Lost on Road rows — one row per shipment sitting in the Lost
        # bucket. Used by the Scorecard Exceptions tool to review the
        # week's LoR candidates. IMPORTANT: the metric must be a
        # row-level one. ``lmdx_conc_lost_on_road_total_dpmo`` (the
        # scorecard's DPMO ratio) is an AGGREGATE metric — bulk.json
        # answers HTTP 500 after ~50s for it, which (x3 retries x day)
        # hung the tool for minutes and surfaced as a fake "PM not
        # connected". ``ssh_lost_shipments`` is the row-level source
        # (native schema: event_datetime, delivery_station_code,
        # tracking_id, ship_method, status_code, reason_code, country).
        # No ``keys`` restriction on purpose so PerfectMile returns the
        # full native column set (same gotcha as ``rts_returns``).
        "metric_name": "ssh_lost_shipments",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Lost on Road rows (ssh_lost_shipments) used by Scorecard Exceptions.",
    },
    "daily_dnr_weekly": {
        # AMZL-only DNR concessions used by the Daily Meeting tool.
        # The dashboard URL the operator used to open by hand is
        # https://perfectmile.amazon.com/dashboards/97030/country/ES/daily?tab=443435
        #     &start-date=...&end-date=...&drilldowns=location
        #     &filter-in-parent-location=%5BDCZ4%5D&dnr_concessions_only_amzl
        # which maps to the ``dnr_concessions_only_amzl`` metric. The
        # row shape matches the legacy ``load_dnr`` parser so the
        # Daily engine can consume it directly (TrackingId / BucketL3
        # / DSP_Short).
        "metric_name": "dnr_concessions_only_amzl",
        "keys": CONCESSION_KEYS,
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Daily Meeting weekly DNR concessions (dnr_concessions_only_amzl).",
    },
    # ------------------------------------------------------------------
    # Pre-Pre-DNR report buckets (DNRs tool). These replace the manual
    # QuickSight CSV exports the operator used to drag into the tool.
    # NOTE: no ``keys`` restriction on purpose — the parser relies on
    # the native column schema for each metric (tracking_id,
    # attempted_delivery_datetime, lt_attempt_shipment_reason,
    # lt_dispatch_pro_company_name, route_code for 2.2; distance_meters,
    # total_call_duration_seconds, contact_event, da_tenure_days for
    # 2.3 / 2.4). Pinning ``keys`` would silently drop those columns
    # (same gotcha documented for ``rts_returns``).
    "dnr_prepre_main": {
        # Bucket 2.2 — DA-level delivery events.
        "metric_name": "dnr_concessions_only_amzl",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Pre-Pre-DNR Bucket 2.2 (dnr_concessions_only_amzl).",
    },
    "dnr_prepre_failures": {
        # Bucket 2.3 — delivered-with-failures signals (distance/calls).
        "metric_name": "dnr_w_delivered_with_failures",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Pre-Pre-DNR Bucket 2.3 (dnr_w_delivered_with_failures).",
    },
    "dnr_prepre_signals": {
        # Bucket 2.4 — delivered-with-signals fallback.
        "metric_name": "dnr_w_delivered_with_signals",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Pre-Pre-DNR Bucket 2.4 (dnr_w_delivered_with_signals).",
    },
    "vsa_audits": {
        # VSA (Vehicle Safety Audit) totals used by the QR VSA tool for the
        # headline "Total" count. No ``keys`` restriction on purpose so
        # PerfectMile returns the full native column set (same rationale as
        # ``rts_returns``).
        "metric_name": "vsaauditstotal",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "VSA audits total (vsaauditstotal) used by the QR VSA tool (Total KPI).",
    },
    "vsa_missing": {
        # Missing VSA audits — the base metric for the QR VSA table. One row
        # per vehicle still pending its audit; the native schema exposes the
        # DSP / VIN / VRN columns the tool detects. No ``keys`` restriction so
        # PerfectMile returns the full native column set.
        "metric_name": "missing_vsa_audits",
        "page_size": 5000,
        "pages_per_request": 1,
        "description": "Missing VSA audits (missing_vsa_audits) — base for the QR VSA table.",
    },
}


def register_metric(key: str, **definition: Any) -> None:
    """Allow tools to register new metrics at startup."""
    METRIC_CATALOG[str(key).strip().lower()] = {**definition}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PerfectMileError(RuntimeError):
    pass


class PerfectMileAuthError(PerfectMileError):
    pass


class PerfectMileRequestError(PerfectMileError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean(value: object) -> str:
    return str(value or "").strip()


def _tokens(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _clean(value)
        if not token:
            continue
        key = token.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return tuple(out)


def _iter_days(date_start: str, date_end: str) -> list[str]:
    start = _dt.datetime.strptime(date_start, "%Y-%m-%d").date()
    end = _dt.datetime.strptime(date_end, "%Y-%m-%d").date()
    if end < start:
        raise PerfectMileRequestError("La fecha final no puede ser anterior a la inicial.")
    days: list[str] = []
    cur = start
    while cur <= end:
        days.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return days


ProgressCallback = Callable[[str, int, int, str], None]


def _emit(cb: ProgressCallback | None, stage: str, current: int, total: int, detail: str = "") -> None:
    if cb is None:
        return
    try:
        cb(stage, current, total, detail)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Request / result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerfectMileRequest:
    metric_key: str
    metric_name: str
    date_start: str
    date_end: str | None = None
    countries: tuple[str, ...] = (PERFECTMILE_DEFAULT_COUNTRY,)
    stations: tuple[str, ...] = ()
    mothers: tuple[str, ...] = ()
    location_types: tuple[str, ...] = PERFECTMILE_DEFAULT_LOCATION_TYPES
    keys: tuple[str, ...] = ()
    page_size: int = 5000
    pages_per_request: int = 1
    mapping: str = PERFECTMILE_DEFAULT_MAPPING
    country_filter_key: str = "Country"
    exclude_filters: dict[str, Sequence[str]] = field(default_factory=lambda: {"Country": ["XX"]})
    extra_filters: dict[str, Sequence[str]] = field(default_factory=dict)

    @property
    def effective_date_end(self) -> str:
        return self.date_end or self.date_start

    @classmethod
    def from_catalog(
        cls,
        metric_key: str,
        date_start: str,
        date_end: str | None = None,
        *,
        countries: Sequence[str] | None = None,
        stations: Sequence[str] | None = None,
        mothers: Sequence[str] | None = None,
        extra_filters: dict[str, Sequence[str]] | None = None,
    ) -> "PerfectMileRequest":
        key = str(metric_key or "").strip().lower()
        if key not in METRIC_CATALOG:
            raise PerfectMileRequestError(f"Metrica PerfectMile no soportada: {metric_key}")
        meta = METRIC_CATALOG[key]
        merged_extra = {**dict(meta.get("extra_filters") or {}), **dict(extra_filters or {})}
        # Auto-default ``stations`` to the operator's STATION_CODE when
        # the caller didn't pass one. This is the country-agnostic path:
        # the bulk endpoint resolves the country from the location, so
        # the same Toolbox works in ES / FR / DE / ... as long as the
        # workbook has the right STATION_CODE.
        resolved_stations = _tokens(stations or ())
        if not resolved_stations:
            from app.core.station_context import resolve_station_code

            station_env = resolve_station_code(
                default=(os.environ.get("STATION_CODE", "") or "").strip().upper()
            )
            if station_env:
                resolved_stations = (station_env,)
        return cls(
            metric_key=key,
            metric_name=str(meta["metric_name"]),
            date_start=date_start,
            date_end=date_end,
            countries=_tokens(countries or (PERFECTMILE_DEFAULT_COUNTRY,)),
            stations=resolved_stations,
            mothers=_tokens(mothers or ()),
            location_types=_tokens(meta.get("location_types", PERFECTMILE_DEFAULT_LOCATION_TYPES)),
            keys=tuple(str(k) for k in meta.get("keys", ()) if str(k).strip()),
            page_size=int(meta.get("page_size", 5000)),
            pages_per_request=int(meta.get("pages_per_request", 1)),
            mapping=str(meta.get("mapping", PERFECTMILE_DEFAULT_MAPPING)),
            country_filter_key=str(meta.get("country_filter_key", "Country")),
            exclude_filters={k: list(v) for k, v in dict(meta.get("exclude_filters", {"Country": ["XX"]})).items()},
            extra_filters={k: list(v) for k, v in merged_extra.items()},
        )

    def build_filters_json(self) -> str:
        """Return the JSON filters payload for the bulk endpoint.

        Station-only mode: when ``self.stations`` is set we filter
        **exclusively** by ``Location`` (and any explicit
        ``extra_filters``). The ``Country`` /
        ``location_continental_region`` / ``location_type`` fields are
        omitted on purpose: PerfectMile resolves the station to its
        country/region server-side, and including those filters
        forces the dashboard into a single-country scope. By dropping
        them, the same Toolbox build works the moment a DS in another
        country is added — only ``STATION_CODE`` needs to change.

        When the operator has not configured a station yet (or the
        caller deliberately wants country-wide data) we keep the
        legacy country/region/location-type filters so PerfectMile
        does not return an unbounded global query.
        """
        if self.stations:
            filters: dict[str, Sequence[str]] = {
                "Location": list(self.stations),
            }
            if self.mothers:
                filters["parent_location"] = list(self.mothers)
            for key, values in self.extra_filters.items():
                cleaned = [_clean(v) for v in values if _clean(v)]
                if cleaned:
                    filters[key] = cleaned
            return json.dumps(filters)

        # Station not pinned: fall back to the legacy country-scoped
        # filter set so we never blindly query everything.
        filters = {
            "location_continental_region": ["EU"],
            self.country_filter_key: list(self.countries or (PERFECTMILE_DEFAULT_COUNTRY,)),
            "location_type": list(self.location_types or PERFECTMILE_DEFAULT_LOCATION_TYPES),
        }
        if self.mothers:
            filters["parent_location"] = list(self.mothers)
        for key, values in self.extra_filters.items():
            cleaned = [_clean(v) for v in values if _clean(v)]
            if cleaned:
                filters[key] = cleaned
        return json.dumps(filters)

    def build_exclude_filters_json(self) -> str:
        # Same logic as the include filters: when we pin a station, we
        # do not include the ``Country`` filter, so the matching
        # ``Country: ["XX"]`` exclude is also pointless. Drop it so
        # the JSON stays minimal and consistent.
        excludes = dict(self.exclude_filters)
        if self.stations and excludes.get("Country") == ["XX"]:
            excludes.pop("Country", None)
        payload = {k: [_clean(v) for v in values if _clean(v)] for k, values in excludes.items()}
        return json.dumps(payload)


@dataclass
class PerfectMileResult:
    request: PerfectMileRequest
    rows: list[dict[str, Any]]
    keys: list[str]
    fetched_at: str
    total_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_key": self.request.metric_key,
            "metric_name": self.request.metric_name,
            "date_start": self.request.date_start,
            "date_end": self.request.effective_date_end,
            "keys": list(self.keys),
            "rows": list(self.rows),
            "total_rows": self.total_rows or len(self.rows),
            "fetched_at": self.fetched_at,
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _is_auth_failure(response: requests.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "json" in content_type:
        return False
    body = (response.text or "")[:400].lower()
    return "<html" in body or "signin" in body or "login" in body


def _build_params(request: PerfectMileRequest, start_day: str, end_day: str, page: int) -> dict[str, str]:
    page_end = page + max(request.pages_per_request, 1) - 1
    params: dict[str, str] = {
        "metric_name": request.metric_name,
        "datetime_start": f"{start_day}T00:00",
        "datetime_end": f"{end_day}T23:59",
        "filters": request.build_filters_json(),
        "exclude_filters": request.build_exclude_filters_json(),
        "mapping": request.mapping,
        "page_range": f"{page}..{page_end}",
        "page_size": str(request.page_size),
        "is_display_restricted_columns": "false",
    }
    if request.keys:
        params["keys"] = json.dumps(list(request.keys))
    return params


def _session_from_cookies(cookies: dict[str, str]) -> requests.Session:
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".amazon.com")
        session.cookies.set(name, value, domain="perfectmile.amazon.com")
    session.headers.update(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "User-Agent": "Mozilla/5.0 ToolBox-PerfectMile",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def healthcheck(cookies: dict[str, str]) -> bool:
    # Caravan-only cutover: this compatibility surface intentionally never
    # probes the retired host. Callers must use the typed Caravan facade.
    return False


def read_cookies() -> dict[str, str]:
    """Return the PerfectMile cookies from the user's Firefox profile."""
    try:
        return perfectmile_cookies_from_profile() or {}
    except Exception:
        return {}


def has_session() -> bool:
    cookies = read_cookies()
    if not cookies:
        return False
    return healthcheck(cookies)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_metric(
    metric: str | PerfectMileRequest,
    *,
    date_start: str | None = None,
    date_end: str | None = None,
    countries: Sequence[str] | None = None,
    stations: Sequence[str] | None = None,
    mothers: Sequence[str] | None = None,
    extra_filters: dict[str, Sequence[str]] | None = None,
    progress_cb: ProgressCallback | None = None,
    cookies: dict[str, str] | None = None,
    verify_session: bool = True,
    single_window: bool = False,
    max_rows: int | None = None,
) -> PerfectMileResult:
    """Fetch a PerfectMile metric using Firefox cookies.

    ``metric`` can be a ``metric_key`` from :data:`METRIC_CATALOG` or a
    pre-built :class:`PerfectMileRequest`. When it's a key, the caller
    must pass ``date_start`` at minimum.

    ``verify_session`` runs a healthcheck before querying. Callers that
    fire several metrics back to back can validate the session once and
    pass ``verify_session=False`` (with explicit ``cookies``) on the
    remaining calls to avoid the extra round trip.

    ``max_rows`` caps the number of rows collected and *stops paginating*
    as soon as the cap is reached. This is critical for high-volume
    metrics (FDDS, On Time Dispatch, volume counts) whose row-level
    payload is every shipment for the window: without it the client
    keeps requesting 5000-row pages sequentially until the whole dataset
    is downloaded, which can take minutes and looks like a hang. ``None``
    or ``<= 0`` means "no cap" (legacy behaviour).
    """
    # The legacy transport was retired. Keeping this public signature avoids
    # import failures in older handlers, but it must not build a session or
    # emit a request to bulk.json. Those handlers are migrated independently
    # to ``app.services.caravan_service`` and fail closed until then.
    raise PerfectMileRequestError(
        "PerfectMile legacy is retired; the Caravan profile is unavailable."
    )

    if isinstance(metric, PerfectMileRequest):
        request = metric
    else:
        if not date_start:
            raise PerfectMileRequestError("date_start es obligatorio cuando se pasa un metric_key.")
        request = PerfectMileRequest.from_catalog(
            metric,
            date_start=date_start,
            date_end=date_end,
            countries=countries,
            stations=stations,
            mothers=mothers,
            extra_filters=extra_filters,
        )

    session_cookies = cookies if cookies is not None else read_cookies()
    if not session_cookies:
        raise PerfectMileAuthError(
            "No hay cookies de PerfectMile en Firefox. Abre "
            f"{PERFECTMILE_HOME_URL} en Firefox, inicia sesion y reintenta."
        )
    if verify_session and not healthcheck(session_cookies):
        raise PerfectMileAuthError(
            "La sesion de PerfectMile ha expirado. Refresca la pagina en Firefox y reintenta."
        )
    session = _session_from_cookies(session_cookies)

    # ``single_window`` queries the whole [date_start, date_end] range in a
    # single bulk call per page instead of iterating day by day. This is
    # essential for the weekly DNRs tool: a 7-day range otherwise fires 7
    # sequential queries per bucket (x3 buckets), each up to a 120s
    # timeout, which made the fetch hang for minutes. The bulk endpoint
    # accepts a wide datetime window directly, so one call returns the
    # whole week (paginated).
    if single_window:
        windows = [(request.date_start, request.effective_date_end)]
    else:
        windows = [(d, d) for d in _iter_days(request.date_start, request.effective_date_end)]
    total_days = len(windows)
    all_rows: list[dict[str, Any]] = []
    keys: list[str] = []

    # Normalise the row cap. ``<= 0`` disables it (legacy "fetch all").
    row_cap = int(max_rows) if max_rows and int(max_rows) > 0 else None
    cap_reached = False

    for day_idx, (start_day, end_day) in enumerate(windows, start=1):
        if cap_reached:
            break
        window_label = start_day if start_day == end_day else f"{start_day}..{end_day}"
        _emit(
            progress_cb,
            f"PerfectMile {request.metric_key.upper()}",
            day_idx - 1,
            total_days,
            f"Consultando {window_label}",
        )
        page = 1
        fetched_batch = 0
        while True:
            params = _build_params(request, start_day, end_day, page)
            response = None
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    response = session.get(
                        PERFECTMILE_BULK_URL,
                        params=params,
                        timeout=PERFECTMILE_DEFAULT_TIMEOUT_SEC,
                        verify=False,
                        allow_redirects=True,
                    )
                except requests.RequestException as exc:
                    # PerfectMile intermittently resets the connection
                    # (ConnectionResetError 10054). Retry a couple of
                    # times with a short backoff before giving up so a
                    # single transient drop does not fail the fetch.
                    last_exc = exc
                    response = None
                    if attempt < 2:
                        _time.sleep(1.0 * (attempt + 1))
                        continue
                    break

                # Auth failures are terminal — retrying with the same
                # expired cookies will not help, so surface immediately.
                if _is_auth_failure(response):
                    raise PerfectMileAuthError(
                        "La sesion de PerfectMile ha expirado mientras se consultaba."
                    )
                if response.status_code == 200:
                    break

                # Non-200, non-auth response. PerfectMile's Dive Deep
                # gateway returns transient / non-standard codes (5xx or
                # 600) when a wide query is throttled or times out — the
                # weekly DNR pull (7 days x 3 buckets) hits this far more
                # than the daily tool. Retry a couple of times with a
                # short backoff before giving up.
                last_exc = PerfectMileRequestError(
                    f"PerfectMile devolvio HTTP {response.status_code}. "
                    f"{(response.text or '')[:200]}".strip()
                )
                if attempt < 2:
                    _time.sleep(1.0 * (attempt + 1))
                    continue
            if response is None:
                raise PerfectMileRequestError(
                    f"Error de red consultando PerfectMile: {last_exc}"
                ) from last_exc
            if _is_auth_failure(response):
                raise PerfectMileAuthError("La sesion de PerfectMile ha expirado mientras se consultaba.")
            if response.status_code != 200:
                # Exhausted retries on a non-200 response. Include a
                # snippet of the upstream body so the operator sees why
                # (e.g. a "600" that carries a throttle / timeout note).
                raise PerfectMileRequestError(
                    f"PerfectMile devolvio HTTP {response.status_code}. "
                    f"{(response.text or '')[:200]}".strip()
                )
            try:
                payload = response.json()
            except Exception as exc:
                raise PerfectMileAuthError("PerfectMile devolvio una respuesta no JSON.") from exc
            if not isinstance(payload, dict):
                raise PerfectMileRequestError("PerfectMile devolvio un payload inesperado.")
            if not keys:
                keys = [str(k) for k in payload.get("keys", [])]
            fetched_batch = 0
            # ``pages`` is normally a dict {page_no: [rows...]} but
            # PerfectMile sometimes returns it as a bare list of row
            # batches (e.g. ``or_rts_amzl_nd`` / ``adjusted_pnov_wankel``)
            # or ``null`` for an empty window. Normalise all three shapes
            # so a non-dict payload doesn't raise AttributeError (which
            # surfaced as an HTTP 500 in the Handover "Extraer PNOV y RTS"
            # button).
            pages_obj = payload.get("pages")
            if isinstance(pages_obj, dict):
                page_groups: list = list(pages_obj.values())
            elif isinstance(pages_obj, list):
                page_groups = pages_obj
            else:
                page_groups = []
            for page_rows in page_groups:
                if not isinstance(page_rows, (list, tuple)):
                    continue
                for row in page_rows:
                    if not isinstance(row, dict):
                        continue
                    row_copy = dict(row)
                    row_copy["fetch_date"] = start_day
                    row_copy["metric_key"] = request.metric_key
                    row_copy["metric_name"] = request.metric_name
                    all_rows.append(row_copy)
                    fetched_batch += 1
            # Stop as soon as we have enough rows. High-volume metrics
            # (FDDS / OTD / counts) otherwise paginate through the whole
            # dataset (tens of thousands of rows) which stalls the UI.
            if row_cap is not None and len(all_rows) >= row_cap:
                cap_reached = True
                break
            if fetched_batch < request.page_size * max(request.pages_per_request, 1):
                break
            page += max(request.pages_per_request, 1)
        _emit(
            progress_cb,
            f"PerfectMile {request.metric_key.upper()}",
            day_idx,
            total_days,
            f"{window_label}: {fetched_batch} filas",
        )

    if row_cap is not None and len(all_rows) > row_cap:
        all_rows = all_rows[:row_cap]

    return PerfectMileResult(
        request=request,
        rows=all_rows,
        keys=keys,
        fetched_at=_dt.datetime.now().isoformat(),
        total_rows=len(all_rows),
    )


# ---------------------------------------------------------------------------
# Concession helpers (used by DEX / DSC / concessions summary)
# ---------------------------------------------------------------------------


def split_concession_rows(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets = {"dnr": [], "dmg": [], "late": [], "nd": [], "never": [], "wrong": [], "other": []}
    for row in rows:
        bucket = _clean(row.get("concession_bucket_l1")).lower()
        event = _clean(row.get("concession_event_name")).lower()
        if "delivered to wrong address" in bucket or "wrong_address" in event:
            buckets["wrong"].append(row)
        elif "never received" in bucket or "never_received" in event:
            buckets["never"].append(row)
        elif bucket == "delivered not received" or "not_received" in event:
            buckets["dnr"].append(row)
        elif "damaged" in bucket or "damaged" in event:
            buckets["dmg"].append(row)
        elif bucket == "late delivery":
            buckets["late"].append(row)
        elif "not delivered" in bucket or "undeliverable" in bucket or "not_delivered" in event:
            buckets["nd"].append(row)
        else:
            buckets["other"].append(row)
    return buckets


# ---------------------------------------------------------------------------
# Public listing (for the agent + API)
# ---------------------------------------------------------------------------


def list_metrics() -> list[dict[str, Any]]:
    return [
        {
            "metric_key": key,
            "metric_name": meta.get("metric_name"),
            "description": meta.get("description", ""),
            "keys": list(meta.get("keys", ())),
        }
        for key, meta in METRIC_CATALOG.items()
    ]
