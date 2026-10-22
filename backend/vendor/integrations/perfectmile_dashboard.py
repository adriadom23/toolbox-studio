"""PerfectMile dashboard client (``api.eu.lospolloshermanos.a2z.com``) — DEPRECATED.

This module used to sign AWS SigV4 requests against the PerfectMile dashboard
API (`api.eu.lospolloshermanos.a2z.com`) using temporary credentials handed out
by ``https://perfectmile.amazon.com/services/configuration``. That backend path
is no longer used: it 403s in the target environment and the side-effect
authentication it triggers is not acceptable inside a report-generation
request.

Dashboard retrieval now happens in a user-controlled browser popup on the
frontend. The popup scrapes the rendered DOM and `postMessage`s the rows back
to the Toolbox UI, which then POSTs them to
``POST /api/daily/generate``. See
``.kiro/specs/daily-retrieval-centralization/design.md`` for the full design.

Every public symbol (dataclasses, exceptions, and the three function entry
points) is kept importable with its original signature so downstream
import sites do not break. The function bodies are reduced to a single
``raise NotImplementedError(...)`` pointing at the new flow.

The concessions arm lives in
:mod:`app.integrations.perfectmile_client` and is unaffected.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.integrations.firefox_cookies import perfectmile_cookies_from_profile  # noqa: F401  (kept for callers that still import it from here)


PERFECTMILE_CONFIG_URL = (
    "https://perfectmile.amazon.com/services/configuration"
    "?services=los_pollos_hermanos"
)
LPH_DEFAULT_SERVICE = "execute-api"
LPH_DEFAULT_REGION = "eu-west-1"
LPH_ARN_TEMPLATE = "arn:lospolloshermanos:metric:{region}::{metric_name}"
LPH_METRIC_PATH = "/metrics/{arn_encoded}/data"
LPH_MAPPING_DEFAULT = "perfectmile-node-mapping-prod"
LPH_CONFIG_TIMEOUT = 30
LPH_METRIC_TIMEOUT = 20


_DEPRECATION_MESSAGE = (
    "PerfectMile dashboard retrieval moved to frontend popup "
    "(see .kiro/specs/daily-retrieval-centralization/design.md). "
    "Use POST /api/daily/generate with perfectmile_rows instead."
)


class DashboardError(RuntimeError):
    pass


class DashboardAuthError(DashboardError):
    pass


@dataclass
class DashboardSession:
    access_key: str
    secret_key: str
    session_token: str
    api_key: str
    endpoint: str
    region: str
    username: str
    expiration: _dt.datetime

    def is_fresh(self, margin: int = 300) -> bool:
        """True if the credentials will still be valid in ``margin`` seconds."""
        now = _dt.datetime.now(_dt.timezone.utc)
        if self.expiration.tzinfo is None:
            expires = self.expiration.replace(tzinfo=_dt.timezone.utc)
        else:
            expires = self.expiration
        return (expires - now).total_seconds() > margin


@dataclass
class DashboardDataPoint:
    date: _dt.date
    value: float | None
    quantity: float | None
    ponderation: float | None
    dimensionality: dict[str, str] = field(default_factory=dict)

    def dsp(self) -> str:
        """Convenience accessor for the ``dsp`` dimension (uppercased)."""
        return str(self.dimensionality.get("dsp") or "").strip().upper()


@dataclass
class DashboardSeries:
    metric_name: str
    periodicity: str
    value_type: str
    data_points: list[DashboardDataPoint] = field(default_factory=list)
    filter_in: dict[str, Any] = field(default_factory=dict)
    detail_dimensions: list[str] = field(default_factory=list)

    def latest(self) -> DashboardDataPoint | None:
        if not self.data_points:
            return None
        return max(self.data_points, key=lambda dp: dp.date)


def fetch_dashboard_session(
    cookies: dict[str, str] | None = None,
    *,
    force_refresh: bool = False,
) -> DashboardSession:
    """Deprecated. See module docstring."""
    raise NotImplementedError(_DEPRECATION_MESSAGE)


def fetch_metric_series(
    metric_name: str,
    start_date: _dt.date,
    end_date: _dt.date,
    *,
    country: str = "ES",
    location: str = "DCZ4",
    periodicity: str = "DAILY",
    mapping: str = LPH_MAPPING_DEFAULT,
    session: DashboardSession | None = None,
    cookies: dict[str, str] | None = None,
    timeout: int = LPH_METRIC_TIMEOUT,
    detail_dimensions: Sequence[str] | None = None,
    secondary_filter_in: dict[str, Any] | None = None,
) -> DashboardSeries:
    """Deprecated. See module docstring."""
    raise NotImplementedError(_DEPRECATION_MESSAGE)


def has_dashboard_session(cookies: dict[str, str] | None = None) -> bool:
    """Deprecated. See module docstring."""
    raise NotImplementedError(_DEPRECATION_MESSAGE)
