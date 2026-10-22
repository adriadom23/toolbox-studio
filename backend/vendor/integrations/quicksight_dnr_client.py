"""QuickSight "DSC Detail View" — DNR concessions per tracking id and week.

Same dashboard as :mod:`app.integrations.quicksight_dsc_client` (which only
serves the DSC bucket cross-match); this module streams the rows OTR Live's
Quality tab needs. Ids verified live 2026-10-16 (DCZ4) via
``GetSheetForDashboard``:

* dashboard ``cf1670d6-…``, sheet ``…_770943c1-…`` ("DSC Detail View")
* visual ``…_db6d1f31-…`` ("Delivery Success Conditions Detail View",
  TABLE), stream source ``d85e6628-…`` (its ``preparedDataSourceId``)
* columns ``3f7992d8-….<name>``: station, year, week (Amazon week: the
  Sunday belongs to the next ISO week), dsp (short code), tracking_id,
  dnr_date, delivery_date, dsc_bucket, shipment_reason …

A stream frame caps at 2048 rows, so the fetch is ONE query per week
(a DS week is ~300 DNRs).
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.integrations import quicksight_robl_client as _qs
from app.integrations.quicksight_robl_client import QuickSightAuthError, QuickSightError

DASHBOARD_ID = "cf1670d6-d3b1-4c97-954d-c644bb0d9a66"
SHEET_ID = f"{DASHBOARD_ID}_770943c1-9eb9-47f2-9622-d3d6ac8a68c7"
VISUAL_ID = f"{DASHBOARD_ID}_db6d1f31-68b5-47f6-b3e6-7293faa0289e"
STREAM_SOURCE_ID = "d85e6628-cdd9-464e-8be7-5d698544a9a4"
_P = "3f7992d8-9d44-4524-bd8e-b4aa7da2f7a1"

LOGIN_URL = f"https://{_qs.HOST}/sn/account/{_qs.ALIAS}/dashboards/{DASHBOARD_ID}/sheets/{SHEET_ID}"

_COLUMNS = ("tracking_id", "dsp", "dnr_date", "delivery_date", "dsc_bucket", "shipment_reason")

# Closed weeks barely move; the current one is refreshed often.
_TTL_PAST_S = 6 * 3600
_TTL_CURRENT_S = 15 * 60
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LOCK = threading.Lock()


def _filter_in(name: str, value: Any) -> str:
    payload = f"[{int(value)}]" if isinstance(value, int) else f'["{value}"]'
    return f"((in({{{_P}.{name}}},{payload},true)))"


def _fetch_week(station: str, year: int, week: int, cookies: dict[str, str], mbtc: str, timeout: float) -> list[dict[str, Any]]:
    group = [f"{_P}.{c}" for c in _COLUMNS]
    start = _qs._build_start_vis(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        visual_id=VISUAL_ID,
        stream_source_id=STREAM_SOURCE_ID,
        group_cols=group,
        metric_cols=[],
        filters=[_filter_in("station", station), _filter_in("year", year), _filter_in("week", week)],
        sort_col=f"{_P}.tracking_id",
        vtype="TABLE",
    )
    msg = _qs._stream_vis(start, cookies, mbtc, timeout)
    rows = []
    for item in msg.get("data", []):
        values = item.get("group", []) or []
        record = {name: (values[i] if i < len(values) else None) for i, name in enumerate(_COLUMNS)}
        record["year"], record["week"] = year, week
        rows.append(record)
    return rows


def fetch_dnr_weeks(
    station: str,
    weeks: list[tuple[int, int]],
    *,
    current: tuple[int, int] | None = None,
    refresh: bool = False,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """DNR rows of ``station`` for every ``(year, week)`` (cached per week).

    Raises :class:`QuickSightAuthError` without a live Firefox session and
    :class:`QuickSightError` on stream failures.
    """
    station = (station or "").strip().upper()
    if not station:
        raise QuickSightError("Falta la estacion para el fetch de DNRs.")
    now = time.time()
    out: list[dict[str, Any]] = []
    missing: list[tuple[int, int]] = []
    with _LOCK:
        for wk in weeks:
            key = f"{station}|{wk[0]}|{wk[1]}"
            ttl = _TTL_CURRENT_S if wk == current else _TTL_PAST_S
            hit = _CACHE.get(key)
            if hit and not refresh and now - hit[0] < ttl:
                out.extend(hit[1])
            else:
                missing.append(wk)
    if not missing:
        return out
    cookies, mbtc = _qs._open_session(DASHBOARD_ID, SHEET_ID, timeout)
    for year, week in missing:
        rows = _fetch_week(station, year, week, cookies, mbtc, timeout)
        with _LOCK:
            _CACHE[f"{station}|{year}|{week}"] = (time.time(), rows)
        out.extend(rows)
    return out


def dnr_login_status() -> dict[str, Any]:
    from app.integrations.quicksight_scheduling_client import dashboard_login_status

    return dashboard_login_status(
        dashboard_id=DASHBOARD_ID,
        sheet_id=SHEET_ID,
        login_url=LOGIN_URL,
        page_url=_qs._page_url(DASHBOARD_ID),
    )


__all__ = [
    "DASHBOARD_ID",
    "LOGIN_URL",
    "QuickSightAuthError",
    "QuickSightError",
    "dnr_login_status",
    "fetch_dnr_weeks",
]
