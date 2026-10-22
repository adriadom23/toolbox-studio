"""Historical PerfectMile overflow-rate input for Forecast UTR."""
from __future__ import annotations

from datetime import date, timedelta
from statistics import fmean
from typing import Any

from app.integrations.perfectmile_caravan_client import (
    CARAVAN_SITE,
    CaravanAuthError,
    CaravanError,
    get_metric_value,
)

METRIC_NAME = "in_overflow_rate"
DEFAULT_WEEKS = 4


def _flatten(value: object):  # type: ignore[no-untyped-def]
    if isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    elif isinstance(value, dict):
        yield value


def _percent(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number * 100.0 if number <= 1.5 else number


def fetch_average(
    station: str,
    *,
    reference: date | None = None,
    weeks: int = DEFAULT_WEEKS,
) -> dict[str, Any]:
    """Average the last complete Sunday-Saturday weekly KPI values."""
    station = str(station or "").strip().upper()
    if not station:
        raise CaravanError("Falta la estacion para consultar in_overflow_rate.")
    weeks = max(1, min(int(weeks or DEFAULT_WEEKS), 12))
    today = reference or date.today()
    current_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    points: list[dict[str, Any]] = []
    # Search up to three times the requested window so missing weeks do not
    # silently turn a "4 week" average into a 1-3 week average.
    for offset in range(1, weeks * 3 + 1):
        if len(points) >= weeks:
            break
        start = current_sunday - timedelta(weeks=offset)
        end = start + timedelta(days=6)
        responses = get_metric_value(
            METRIC_NAME,
            start.isoformat(),
            end.isoformat(),
            station=station,
            periodicity="WEEKLY",
        )
        candidates: list[float] = []
        for item in _flatten(responses):
            metric_value = item.get("metricData", {}).get("metricValue", {})
            value = _percent(metric_value.get("value"))
            if value is not None:
                candidates.append(value)
        if not candidates:
            continue
        points.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "value": fmean(candidates),
        })
    average = fmean(point["value"] for point in points) if points else None
    return {
        "station": station,
        "metric": METRIC_NAME,
        "weeks_requested": weeks,
        "weeks_used": len(points),
        "points": points,
        "average_pct": average,
        "source": "perfectmile_caravan",
    }


__all__ = [
    "CARAVAN_SITE",
    "CaravanAuthError",
    "CaravanError",
    "DEFAULT_WEEKS",
    "METRIC_NAME",
    "fetch_average",
]
