"""QuickSight SPZ/OV planning inputs for Forecast UTR.

Source: dashboard ``SPZ & OV Solve``, sheet ``Latest plan - OV solve`` and
visual ``Full details of OV solve``. The OV percentage is reproduced from the
physical columns used by the dashboard calculated field:

    sum(dispatched_shipments * ov_package_perc) / sum(dispatched_shipments)

The query groups those physical values so the ratio can be rebuilt in Python
without relying on a calculated-field UUID that the stream API rejects.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.integrations.quicksight_robl_client import (
    _build_start_vis,
    _filter_in,
    _open_session,
    _rows_from_frame,
    _stream_vis,
)
from app.integrations.quicksight_scheduling_client import (
    ALIAS,
    HOST,
    QuickSightAuthError,
    QuickSightError,
    dashboard_login_status,
)

DASHBOARD_ID = "8bbfc65a-acc0-4d35-b79a-10ef0bba96ed"
SHEET_ID = f"{DASHBOARD_ID}_81478bfa-3cf3-470e-8ae9-cb67a96c8ea8"
VISUAL_ID = f"{DASHBOARD_ID}_70224f2a-3bd2-4ace-aeb5-baad1f245b06"
STREAM_SOURCE_ID = "155aefae-baf4-495e-87a3-2482a1fd12b8"
LOGIN_URL = (
    f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}"
    f"/sheets/{SHEET_ID}"
)

_SPZ = "435c3229-9352-40fb-9d1b-0427dd555ef0"
_OV = "5692556d-f9db-4a33-b060-c9f542f79c41"
_C_COUNTRY = f"{_SPZ}.country"
_C_STATION = f"{_SPZ}.mother_ds"
_C_DATE = f"{_OV}.Delivery day"
_C_ENDCAPS = f"{_OV}.EndCaps needed?"
_C_MIN_PMET = f"{_OV}.Min P-met tag"
_C_SHIPMENTS = f"{_SPZ}.dispatched_shipments"
_C_OV_PCT = f"{_SPZ}.ov_package_perc"
_C_FINAL_SPZ = f"{_SPZ}.Final_SPZ"
_C_CYCLE = f"{_SPZ}.cycle"
_C_STATION_LEVEL = f"{_OV}.Station_level_analysis"

_GROUP_COLS = [
    _C_COUNTRY,
    _C_STATION,
    _C_DATE,
    _C_ENDCAPS,
    _C_MIN_PMET,
    _C_SHIPMENTS,
    _C_OV_PCT,
]


def login_status() -> dict[str, Any]:
    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=f"https://{HOST}/sn/account/{ALIAS}/dashboards/{DASHBOARD_ID}",
    )


def _request(station: str) -> dict[str, Any]:
    filters = [
        _filter_in(_C_STATION, [station]),
        _filter_in(_C_CYCLE, ["cycle_1"]),
        _filter_in(_C_STATION_LEVEL, ["Y"]),
    ]
    return _build_start_vis(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        visual_id=VISUAL_ID,
        stream_source_id=STREAM_SOURCE_ID,
        group_cols=_GROUP_COLS,
        metric_cols=[(_C_FINAL_SPZ, "SUM")],
        filters=filters,
        sort_col=_C_DATE,
        sort_dir="DESC",
        limit=50000,
        vtype="TABLE",
    )


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def fetch_daily(station: str, *, timeout: float = 45.0) -> dict[str, Any]:
    """Return daily dashboard values as ``[{date, spz, ov_pct}]``.

    Multiple physical rows for one day are combined with dispatched shipments
    as weight. This mirrors the dashboard's OV formula and avoids a simple
    average when the source is split across hidden dimensions.
    """
    station = str(station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para consultar SPZ/OV.")
    cookies, mbtc = _open_session(DASHBOARD_ID, SHEET_ID, timeout)
    frame = _stream_vis(_request(station), cookies, mbtc, timeout)
    raw = _rows_from_frame(
        frame,
        ["country", "station", "date", "endcaps", "min_pmet", "shipments", "ov_perc"],
        [(_C_FINAL_SPZ, "spz")],
    )

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw:
        if str(row.get("station") or "").strip().upper() != station:
            continue
        day = str(row.get("date") or "")[:10]
        if day:
            buckets[day].append(row)

    rows: list[dict[str, Any]] = []
    for day, items in buckets.items():
        total_weight = 0.0
        ov_weighted = 0.0
        spz_weighted = 0.0
        spz_weight = 0.0
        for item in items:
            weight = _number(item.get("shipments"))
            ov = _number(item.get("ov_perc"))
            spz = _number(item.get("spz"))
            if weight is not None and weight > 0 and ov is not None:
                total_weight += weight
                ov_weighted += weight * ov
            if spz is not None and spz > 0:
                safe_weight = weight if weight is not None and weight > 0 else 1.0
                spz_weight += safe_weight
                spz_weighted += safe_weight * spz
        ov_ratio = ov_weighted / total_weight if total_weight > 0 else None
        rows.append({
            "date": day,
            "spz": spz_weighted / spz_weight if spz_weight > 0 else None,
            "ov_pct": ov_ratio * 100.0 if ov_ratio is not None else None,
            "source_rows": len(items),
        })
    rows.sort(key=lambda row: row["date"])
    return {"station": station, "rows": rows, "source": "quicksight_spz_ov"}


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_daily",
    "login_status",
]
