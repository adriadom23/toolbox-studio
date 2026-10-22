"""CloudWatch internal dashboards client — ``cw-dashboards.aka.amazon.com``.

Pulls CloudWatch ``GetMetricData`` results the same way the internal
CloudWatch console does, so tools can read automation metrics server-side
instead of asking the operator to eyeball a dashboard.

The console never holds AWS credentials in the browser. Instead it:

1. Loads a dashboard page (``/cloudwatch/dashboardInternal``) that embeds a
   ``<meta name="csrf" content="...">`` token.
2. POSTs the metric requests it wants to run to its OWN backend
   (``/cloudwatch/CloudWatch/data/auth.V4BatchSign/...``) with the Midway
   session cookies + csrf token. The backend assumes the dashboard's account
   role (STS) and returns SigV4-signed headers per request.
3. POSTs each signed request straight to
   ``monitoring.<region>.amazonaws.com`` (``GetMetricData``).

We replicate exactly that flow using the operator's Firefox ``cw-dashboards``
cookies (Midway). Auth mirrors the SCC / Cortex clients:

* :class:`CwDashAuthError` / :class:`CwDashClientError`
* :func:`cookies_status`
* :class:`CwDashboardsClient` — ``get_metric_data`` + the ADTA helper.
* :func:`fetch_adta_quality` — convenience ``{success, operational, machine}``.

ADTA (Automation Sorter) quality comes from the ``Automation-Sorter-<STATION>``
dashboard's "QUALITY / ERRORS" widget: it classifies the GreengrassV2 sorter
``MetricName`` counts into success / operational-error / machine-error and
returns their share of the total. We reproduce that metric-math query,
parametrised by station (siteId) and time window.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

import requests

from app.integrations.firefox_cookies import (
    CWDASH_HOST,
    CWDASH_REQUIRED_COOKIES,
    cwdash_cookies_from_profile,
    has_required,
)

CWDASH_BASE_URL = f"https://{CWDASH_HOST}"
CWDASH_DASHBOARD_PATH = "/cloudwatch/dashboardInternal"
# The parent dashboard the station sorter widgets live under. The account +
# partition are fixed for the RME automation dashboards.
CWDASH_ACCOUNT_ID = "015435777028"
CWDASH_PARENT_DASHBOARD = "RME-Automation-Machine-Error-DMV3"
CWDASH_REGION = "eu-west-1"
CWDASH_MONITORING_HOST = f"monitoring.{CWDASH_REGION}.amazonaws.com"
CWDASH_DEFAULT_TIMEOUT = 45.0

# The per-station sorter dashboard name: the station code is appended (single
# station code from config, NOT the double station code).
CWDASH_SORTER_DASHBOARD_PREFIX = "Automation-Sorter-"

# GreengrassV2 sorter ``MetricName`` classification, captured from the
# Automation-Sorter dashboard's "QUALITY / ERRORS" widget.
ADTA_SUCCESS = ("SUCCESS",)
ADTA_MACHINE_ERRORS = (
    "UNKNOWN", "UNEXPECTED_CONTAINER", "TRACKING_ERROR", "GAP_ERROR",
    "DESTINATION_NON_OPERATIONAL", "NO_CODE", "FAILED_TO_DIVERT",
    "NO_DESTINATION_RECEIVED", "LOST_CONTAINER", "CONTAINER_UTILIZATION",
    "UNABLE_TO_DIVERT",
)
ADTA_OPERATIONAL_ERRORS = (
    "DESTINATION_FULL", "DESTINATION_DISABLED", "WEIGHT_ERROR",
    "DESTINATION_NOT_ATTEMPTED", "INVALID_DESTINATION", "NO_READ",
    "MULTI_LABEL", "THROUGHPUT_LIMIT", "DIMENSION_ERROR",
)
ADTA_PROTOCOL = "sort-2021-v1"

# Manual labeler (Automation Manual Labeler). Captured from its
# "QUALITY / ERRORS" widget: GreengrassV2 counts over the
# ``asml-tcp-ip-v1`` protocol, aggregated across the labeler lines.
CWDASH_MANUAL_LABELER_DASHBOARD_PREFIX = "Automation-Manual-Labeler-"
ASML_PROTOCOL = "asml-tcp-ip-v1"
ASML_LINES = (
    "Line 1", "Line 2", "Line 3", "Line 4", "Line 5", "Line 5 Spare", "Line 6",
)
ASML_TOTAL = ("Total", "MANUAL_INDUCT_TOTAL")
ASML_NO_READ = ("NoRead",)
ASML_MULTI_READ = ("MultiRead",)
ASML_MISAPPLIED = ("FAILURE",)
ASML_SAL_TOTAL = ("SAL_VERIFICATION_TOTAL",)

# ASL (Automation Labeler). The 2026-09-03 CloudWatch HAR confirms the
# ``slam-2021-v1`` protocol and these process/machine error buckets. Queries
# intentionally aggregate every station line: line names vary by site, while
# protocol + siteId are the stable dashboard dimensions.
CWDASH_ASL_DASHBOARD_PREFIX = "Automation-Labeler-"
ASL_PROTOCOL = "slam-2021-v1"
ASL_TOTAL = ("Total",)
ASL_PROCESS_ERRORS = (
    "VERIFY_SALNOREAD", "VERIFY_SHIPNOREAD", "BAD_REQUEST",
    "VERIFY_SALSHIPNOREAD", "VERIFY_SALMISMATCH", "VERIFY_SAL_MULTIREAD",
)
ASL_MACHINE_ERRORS = (
    "PITCH_OR_TRACKING_ERROR", "CAMERA_ERROR_INDUCT", "CAMERA_ERROR_VERIFY",
    "MISMATCH_FLUSH", "PRINTER_ERROR", "TAMP_FAILED_TO_EXTEND",
    "MISC_ERROR_MACHINE",
)

AUTOMATION_SOURCE_PREFIXES = {
    "adta": CWDASH_SORTER_DASHBOARD_PREFIX,
    "manual": CWDASH_MANUAL_LABELER_DASHBOARD_PREFIX,
    "asl": CWDASH_ASL_DASHBOARD_PREFIX,
}
AUTOMATION_SOURCE_ORDER = ("adta", "manual", "asl")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# ``<meta id="csrf" name="csrf" content="....">`` on the dashboard page.
_CSRF_RE = re.compile(r'name="csrf"\s+content="([^"]+)"')


class CwDashAuthError(RuntimeError):
    """Raised when cookies / csrf are missing or the session is rejected."""


class CwDashClientError(RuntimeError):
    """Raised on any other unexpected cw-dashboards / monitoring response."""


# ---------------------------------------------------------------------------
# Cookies / login status
# ---------------------------------------------------------------------------


def _read_cookies() -> dict[str, str]:
    try:
        return cwdash_cookies_from_profile()
    except Exception:  # noqa: BLE001
        return {}


def dashboard_page_url(parent_dashboard: str = CWDASH_PARENT_DASHBOARD) -> str:
    return (
        f"{CWDASH_BASE_URL}{CWDASH_DASHBOARD_PATH}"
        f"?accountId={CWDASH_ACCOUNT_ID}&awsPartition=aws&name={parent_dashboard}"
    )


CWDASH_LOGIN_URL = dashboard_page_url()


def cookies_status() -> dict[str, Any]:
    """Return ``{ready, missing, cookie_count, login_url}`` for cw-dashboards.

    "ready" requires a currently-valid ``amzn_sso_token`` (expired cookies are
    already filtered out on read), since that Midway JWT must be live to load
    the dashboard page we scrape the csrf token from and to authorize the
    ``auth.V4BatchSign`` call.
    """
    cookies = _read_cookies()
    missing = [c for c in CWDASH_REQUIRED_COOKIES if c not in cookies]
    return {
        "ready": bool(cookies) and not missing,
        "missing": missing,
        "cookie_count": len(cookies),
        "login_url": CWDASH_LOGIN_URL,
    }


def sorter_dashboard_name(station: str) -> str:
    """The per-station Automation Sorter (ADTA) dashboard name."""
    return f"{CWDASH_SORTER_DASHBOARD_PREFIX}{(station or '').strip().upper()}"


def labeler_dashboard_name(station: str) -> str:
    """The per-station Automation Manual Labeler dashboard name."""
    return automation_dashboard_name("manual", station)


def asl_dashboard_name(station: str) -> str:
    """The per-station Automation Labeler (ASL) dashboard name."""
    return automation_dashboard_name("asl", station)


def automation_dashboard_name(source: str, station: str) -> str:
    """Return the exact CloudWatch dashboard name for a built-in source."""
    source = str(source or "").strip().lower()
    prefix = AUTOMATION_SOURCE_PREFIXES.get(source)
    if not prefix:
        raise CwDashClientError(f"Fuente de Automation desconocida: {source}")
    return f"{prefix}{(station or '').strip().upper()}"


def _automation_profile_from_text(value: str) -> str | None:
    """Infer a supported metric profile from a dashboard name/body text."""
    text = str(value or "").casefold()
    if ASML_PROTOCOL.casefold() in text or ("manual" in text and "labeler" in text):
        return "manual"
    if ASL_PROTOCOL.casefold() in text or "automation-labeler" in text or "asl" in text:
        return "asl"
    if ADTA_PROTOCOL.casefold() in text or "sorter" in text or "adta" in text:
        return "adta"
    return None


def _source_dashboard(source: str, station: str) -> tuple[str, str | None]:
    """Resolve config token to ``(exact dashboard name, metric profile)``."""
    token = str(source or "").strip()
    builtin = token.lower()
    if builtin in AUTOMATION_SOURCE_PREFIXES:
        return automation_dashboard_name(builtin, station), builtin
    return token, _automation_profile_from_text(token)


def _normalise_timezone(timezone: str | None) -> str | None:
    """Validate a CloudWatch ``LabelOptions.Timezone`` value (``+HHMM``)."""
    value = str(timezone or "").strip()
    match = re.fullmatch(r"([+-])(\d{2})(\d{2})", value)
    if not match:
        return None
    hours, minutes = int(match.group(2)), int(match.group(3))
    if hours > 14 or minutes > 59:
        return None
    return value


def _metric_body(metrics: list[dict[str, Any]], period: int, start: int, end: int,
                 timezone: str | None = None) -> str:
    body: dict[str, Any] = {
        "Metrics": metrics,
        "Defaults": {"Period": period, "Stat": "Sum",
                     "Range": {"StartTime": int(start), "EndTime": int(end)}},
        "MaxDatapoints": 504000,
        "NextToken": None,
    }
    local_timezone = _normalise_timezone(timezone)
    if local_timezone:
        body["LabelOptions"] = {"Timezone": local_timezone}
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Metric-math builders
# ---------------------------------------------------------------------------


def _search_sum(
    site_id: str,
    metric_names: Iterable[str],
    period: int,
    *,
    protocol: str = ADTA_PROTOCOL,
    lines: Iterable[str] | None = None,
) -> str:
    """Build a ``SUM(SEARCH(...))`` GreengrassV2 expression for a station.

    Aggregates the counts for the given ``MetricName`` set across ``site_id``
    for ``protocol``. When ``lines`` is given the search is scoped to those
    ``name`` values (the ASML labeler lines); otherwise every ``name`` matches
    (station-level, used for the ADTA sorter fingers).
    """
    names = " OR ".join(f'MetricName=\\"{n}\\"' for n in metric_names)
    schema = "{GreengrassV2,name,protocol,siteId}"
    line_filter = ""
    if lines:
        line_names = " OR ".join(f'name=\\"{n}\\"' for n in lines)
        line_filter = f"({line_names}) "
    query = (
        f'{schema} siteId=\\"{site_id}\\" {line_filter}({names}) '
        f'protocol=\\"{protocol}\\"'
    )
    return f'SUM(SEARCH("{query}", "Sum", {period}))'


def build_adta_body(site_id: str, start: int, end: int,
                    timezone: str | None = None) -> str:
    """Build the ``GetMetricData`` JSON body for ADTA quality over a window.

    Returns the three shares (success / operational-error / machine-error) of
    the total sorter results for ``site_id`` between ``start`` and ``end``
    (epoch seconds). The whole window is aggregated into a single datapoint.
    """
    start = int(start)
    end = int(end)
    period = max(60, ((end - start) // 60) * 60 or 60)
    metrics = [
        {"Alias": "pct_success", "Expression": "((sum_success)/(sum_total))*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_success", "ReturnData": True},
        {"Alias": "pct_operational_error",
         "Expression": "((sum_op)/(sum_total))*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_operational_error", "ReturnData": True},
        {"Alias": "pct_machine_error",
         "Expression": "((sum_machine)/(sum_total))*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_machine_error", "ReturnData": True},
        {"Alias": "sum_success",
         "Expression": _search_sum(site_id, ADTA_SUCCESS, period, protocol=ADTA_PROTOCOL),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_success", "ReturnData": True},
        {"Alias": "sum_machine",
         "Expression": _search_sum(site_id, ADTA_MACHINE_ERRORS, period, protocol=ADTA_PROTOCOL),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_machine", "ReturnData": True},
        {"Alias": "sum_op",
         "Expression": _search_sum(site_id, ADTA_OPERATIONAL_ERRORS, period, protocol=ADTA_PROTOCOL),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_op", "ReturnData": True},
        {"Alias": "sum_total", "Expression": "sum_success + sum_machine + sum_op",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_total", "ReturnData": True},
    ]
    return _metric_body(metrics, period, start, end, timezone)


def build_asml_body(site_id: str, start: int, end: int,
                    timezone: str | None = None) -> str:
    """Build the ``GetMetricData`` JSON body for ASML quality over a window.

    Returns the shares of the labeler results for ``site_id`` between ``start``
    and ``end`` (epoch seconds): quality (success / total), no-read, multi-read
    (each / total) and misapplied SAL (FAILURE / SAL_VERIFICATION_TOTAL). Raw
    ``sum_total`` is returned too so callers can tell "no data / no ASML at this
    station" apart from a real 0%.
    """
    start = int(start)
    end = int(end)
    period = max(60, ((end - start) // 60) * 60 or 60)

    def s(names):
        return _search_sum(site_id, names, period, protocol=ASML_PROTOCOL, lines=ASML_LINES)

    metrics = [
        {"Alias": "sum_total", "Expression": s(ASML_TOTAL),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_total", "ReturnData": True},
        {"Alias": "sum_no_read", "Expression": s(ASML_NO_READ),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_no_read", "ReturnData": True},
        {"Alias": "sum_multi_read", "Expression": s(ASML_MULTI_READ),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_multi_read", "ReturnData": True},
        {"Alias": "sum_misapplied", "Expression": s(ASML_MISAPPLIED),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_misapplied", "ReturnData": True},
        {"Alias": "sum_sal_total", "Expression": s(ASML_SAL_TOTAL),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_sal_total", "ReturnData": True},
        {"Alias": "pct_quality",
         "Expression": "((sum_total - sum_no_read - sum_multi_read)/sum_total)*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_quality", "ReturnData": True},
        {"Alias": "pct_no_read", "Expression": "(sum_no_read/sum_total)*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_no_read", "ReturnData": True},
        {"Alias": "pct_multi_read", "Expression": "(sum_multi_read/sum_total)*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_multi_read", "ReturnData": True},
        {"Alias": "pct_misapplied", "Expression": "(sum_misapplied/sum_sal_total)*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_misapplied", "ReturnData": True},
    ]
    return _metric_body(metrics, period, start, end, timezone)


def build_asl_body(site_id: str, start: int, end: int,
                   timezone: str | None = None) -> str:
    """Build the Automation Labeler (ASL) quality query for one window."""
    start = int(start)
    end = int(end)
    period = max(60, ((end - start) // 60) * 60 or 60)

    def s(names):
        return _search_sum(site_id, names, period, protocol=ASL_PROTOCOL)

    metrics = [
        {"Alias": "sum_total", "Expression": s(ASL_TOTAL),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_total", "ReturnData": True},
        {"Alias": "sum_process", "Expression": s(ASL_PROCESS_ERRORS),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_process", "ReturnData": True},
        {"Alias": "sum_machine", "Expression": s(ASL_MACHINE_ERRORS),
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "sum_machine", "ReturnData": True},
        {"Alias": "pct_quality",
         "Expression": "((sum_total - sum_process - sum_machine)/sum_total)*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_quality", "ReturnData": True},
        {"Alias": "pct_process", "Expression": "(sum_process/sum_total)*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_process", "ReturnData": True},
        {"Alias": "pct_machine", "Expression": "(sum_machine/sum_total)*100",
         "AccountId": CWDASH_ACCOUNT_ID, "Region": CWDASH_REGION,
         "Id": "pct_machine", "ReturnData": True},
    ]
    return _metric_body(metrics, period, start, end, timezone)


def _first_value(metric_data: list[dict[str, Any]], alias: str) -> float | None:
    for md in metric_data or []:
        if md.get("Alias") == alias or md.get("Id") == alias:
            values = md.get("Values") or []
            if values:
                try:
                    return float(values[0])
                except (TypeError, ValueError):
                    return None
            return None
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CwDashboardsClient:
    """Authenticated cw-dashboards client (one scraped csrf per instance)."""

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        timeout: float = CWDASH_DEFAULT_TIMEOUT,
        parent_dashboard: str = CWDASH_PARENT_DASHBOARD,
    ) -> None:
        self._cookies = cookies if cookies is not None else _read_cookies()
        self.timeout = timeout
        self._parent_dashboard = parent_dashboard
        self._page_url = dashboard_page_url(parent_dashboard)
        self._session = requests.Session()
        if self._cookies:
            self._session.cookies.update(self._cookies)
        self._csrf: str | None = None

    # -- auth -------------------------------------------------------------

    def _ensure_csrf(self) -> str:
        if self._csrf:
            return self._csrf
        if not has_required(self._cookies, CWDASH_REQUIRED_COOKIES):
            raise CwDashAuthError(
                "Sesión de CloudWatch caducada (amzn_sso_token). "
                "Abre/recarga cw-dashboards en Firefox e inicia sesión, luego reintenta."
            )
        try:
            page = self._session.get(
                self._page_url,
                headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            raise CwDashClientError(f"Error SSL contra {CWDASH_HOST}: {exc}") from exc
        except requests.RequestException as exc:
            raise CwDashClientError(f"Error de red contra {CWDASH_HOST}: {exc}") from exc

        if page.status_code in (301, 302, 303, 307, 308, 401, 403):
            raise CwDashAuthError(
                f"CloudWatch redirige/rechaza la página (HTTP {page.status_code}). "
                "Recarga cw-dashboards en Firefox e inicia sesión."
            )
        if page.status_code != 200:
            raise CwDashClientError(
                f"cw-dashboards devolvió HTTP {page.status_code} al cargar la página."
            )
        m = _CSRF_RE.search(page.text or "")
        if not m:
            raise CwDashAuthError(
                "No se encontró el token csrf en cw-dashboards (sesión caducada). "
                "Recarga cw-dashboards en Firefox e inicia sesión."
            )
        self._csrf = m.group(1)
        return self._csrf

    # -- dashboard catalogue ---------------------------------------------

    def _dashboard_call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Call a cw-dashboards proxy API using the shape captured in the HAR."""
        csrf = self._ensure_csrf()
        request_id = f"{int(time.time() * 1000)}-{int(time.time() * 1000000)}"
        url = (
            f"{CWDASH_BASE_URL}/cloudwatch/CloudWatch/data/"
            f"dashboards.{action}/{request_id}"
        )
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "x-csrf-token": csrf,
            "aws-region": CWDASH_REGION,
            "x-amz-accountid": CWDASH_ACCOUNT_ID,
            "x-amz-target": f"dashboards.{action}",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": CWDASH_BASE_URL,
            "Referer": self._page_url,
        }
        try:
            resp = self._session.post(
                url, headers=headers, data=json.dumps(payload),
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CwDashClientError(
                f"Error de red consultando dashboards.{action}: {exc}"
            ) from exc
        if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
            raise CwDashAuthError(
                f"cw-dashboards rechazó dashboards.{action} ({resp.status_code})."
            )
        if resp.status_code != 200:
            raise CwDashClientError(
                f"dashboards.{action} devolvió HTTP {resp.status_code}. "
                f"{(resp.text or '')[:200]}".strip()
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise CwDashClientError(
                f"dashboards.{action} no devolvió JSON válido: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise CwDashClientError(f"dashboards.{action} devolvió una forma inesperada.")
        return data

    def list_dashboard_names(self) -> list[str]:
        """List every dashboard name, following the console pagination."""
        names: list[str] = []
        token: str | None = None
        for _ in range(20):
            data = self._dashboard_call("ListDashboards", {"NextToken": token})
            for entry in data.get("DashboardEntries") or []:
                name = str(entry.get("DashboardName") or "").strip()
                if name:
                    names.append(name)
            token = data.get("NextToken")
            if not token:
                break
        return names

    def describe_dashboard(self, dashboard_name: str) -> dict[str, Any]:
        """Return the parsed widget document for an exact dashboard name.

        The internal console operation is ``dashboards.DescribeDashboard``
        (not GetDashboard). Depending on the proxy layer the JSON string is
        returned as ``Dashboard`` or ``DashboardBody``.
        """
        name = str(dashboard_name or "").strip()
        if not name:
            raise CwDashClientError("Falta el nombre del dashboard de Automation.")
        data = self._dashboard_call("DescribeDashboard", {"DashboardName": name})
        raw: Any = data.get("Dashboard") or data.get("DashboardBody")
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            raise CwDashClientError(f"CloudWatch no devolvio el dashboard {name}.")
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise CwDashClientError(
                f"CloudWatch devolvio un dashboard no interpretable: {name}"
            ) from exc
        if not isinstance(document, dict):
            raise CwDashClientError(f"Dashboard de Automation inesperado: {name}")
        return document

    def resolve_automation_sources(
        self, station: str, configured: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve configured built-ins/custom names against CloudWatch.

        Custom names are exact and may use any casing/spaces. Their metric
        profile is inferred from the name first and, when needed, from the
        dashboard widget document's known automation protocol.
        """
        station = (station or "").strip().upper()
        if not station:
            raise CwDashClientError("Falta el código de estación para Automation.")
        tokens = list(configured or AUTOMATION_SOURCE_ORDER)
        existing = set(self.list_dashboard_names())
        descriptors: list[dict[str, Any]] = []
        for raw_source in tokens:
            source = str(raw_source or "").strip()
            if not source:
                continue
            dashboard, profile = _source_dashboard(source, station)
            available = dashboard in existing
            error = ""
            if available and not profile:
                try:
                    document = self.describe_dashboard(dashboard)
                    profile = _automation_profile_from_text(
                        json.dumps(document, ensure_ascii=False)
                    )
                except CwDashClientError as exc:
                    error = str(exc)
            if available and not profile and not error:
                error = "El dashboard no contiene un perfil Automation compatible."
            descriptors.append({
                "id": source.lower() if source.lower() in AUTOMATION_SOURCE_PREFIXES else source,
                "dashboard": dashboard,
                "profile": profile or "",
                "available": bool(available and profile),
                "error": error,
            })
        return descriptors

    def available_automation_sources(
        self, station: str, configured: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return configured sources that resolve to real supported dashboards."""
        return [
            source for source in self.resolve_automation_sources(station, configured)
            if source.get("available")
        ]

    # -- signing ----------------------------------------------------------

    def _sign(self, bodies: list[str], dashboard_name: str) -> list[dict[str, Any]]:
        """Ask the console backend to SigV4-sign a batch of GetMetricData bodies."""
        csrf = self._ensure_csrf()
        ts = str(int(time.time() * 1000)) + "-" + str(int(time.time() * 1000))
        url = f"{CWDASH_BASE_URL}/cloudwatch/CloudWatch/data/auth.V4BatchSign/{ts}"
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "x-csrf-token": csrf,
            "aws-region": CWDASH_REGION,
            "x-amz-accountid": CWDASH_ACCOUNT_ID,
            "x-amz-target": "auth.V4BatchSign",
            "content-type": "application/json; charset=UTF-8",
            "x-amz-dashboard-name": dashboard_name,
            "x-amz-dashboard-request-types": "all",
            "Origin": CWDASH_BASE_URL,
            "Referer": self._page_url,
        }
        payload = {
            "Requests": [
                {"Body": b, "Endpoint": None, "Method": "GetMetricData",
                 "Service": None, "Region": CWDASH_REGION}
                for b in bodies
            ]
        }
        try:
            resp = self._session.post(
                url, headers=headers, data=json.dumps(payload),
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CwDashClientError(f"Error de red firmando en cw-dashboards: {exc}") from exc

        if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
            raise CwDashAuthError(
                f"cw-dashboards rechazó la firma ({resp.status_code}). "
                "Recarga cw-dashboards en Firefox e inicia sesión."
            )
        if resp.status_code != 200:
            raise CwDashClientError(
                f"auth.V4BatchSign devolvió HTTP {resp.status_code}. "
                f"{(resp.text or '')[:200]}".strip()
            )
        try:
            signed = resp.json()
        except ValueError as exc:
            raise CwDashClientError(f"V4BatchSign no devolvió JSON válido: {exc}") from exc
        if not isinstance(signed, list) or len(signed) != len(bodies):
            raise CwDashClientError("V4BatchSign devolvió un número de firmas inesperado.")
        return signed

    # -- core call --------------------------------------------------------

    def get_metric_data_batch(
        self, bodies: list[str], *, dashboard_name: str,
    ) -> list[list[dict[str, Any]]]:
        """Sign and execute GetMetricData bodies using the console flow.

        The current cw-dashboards HAR signs a batch through
        ``auth.V4BatchSign`` and then posts each signed body directly to the
        regional CloudWatch monitoring endpoint. Monitoring returns
        ``MetricData`` alongside ``Defaults`` and ``Messages``.
        """
        signed = self._sign(bodies, dashboard_name)
        out: list[list[dict[str, Any]]] = []
        for body, signature in zip(bodies, signed):
            headers = {
                "User-Agent": _USER_AGENT,
                "Accept": "*/*",
                "authorization": signature["Authorization"],
                "content-type": "application/x-amz-json-1.0",
                "x-amz-date": signature["Timestamp"],
                "x-amz-console-signature": signature["ConsoleSignature"],
                "x-amz-security-token": signature["SecurityToken"],
                "x-amz-target": signature["Target"],
                "Origin": CWDASH_BASE_URL,
                "Referer": f"{CWDASH_BASE_URL}/",
            }
            host = signature.get("HostName") or CWDASH_MONITORING_HOST
            try:
                response = requests.post(
                    f"https://{host}/",
                    headers=headers,
                    data=body,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise CwDashClientError(f"Error de red contra {host}: {exc}") from exc
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise CwDashAuthError(
                    f"monitoring rechazó la petición ({response.status_code}). "
                    "Recarga cw-dashboards en Firefox e inicia sesión."
                )
            if response.status_code != 200:
                raise CwDashClientError(
                    f"GetMetricData devolvió HTTP {response.status_code}. "
                    f"{(response.text or '')[:200]}".strip()
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise CwDashClientError(
                    f"GetMetricData no devolvió JSON válido: {exc}"
                ) from exc
            out.append(data.get("MetricData") or [] if isinstance(data, dict) else [])
        return out

    # -- typed helpers ----------------------------------------------------

    def fetch_adta_windows(
        self,
        station: str,
        windows: list[tuple[int, int]],
        *,
        timezone: str | None = None,
        dashboard_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ADTA quality/operational/machine shares for each time window.

        ``windows`` is a list of ``(start, end)`` epoch-second pairs. Each entry
        in the result carries the window bounds and the three percentages plus
        the raw counts (``total`` / ``success``) so the caller can tell "no
        data" (total 0) apart from "0% success".
        """
        station = (station or "").strip().upper()
        if not station:
            raise CwDashClientError("Falta el código de estación para ADTA.")
        dash = dashboard_name or sorter_dashboard_name(station)
        bodies = [build_adta_body(station, s, e, timezone) for (s, e) in windows]
        if not bodies:
            return []
        batches = self.get_metric_data_batch(bodies, dashboard_name=dash)
        out: list[dict[str, Any]] = []
        for (start, end), md in zip(windows, batches):
            total = _first_value(md, "sum_total")
            out.append(
                {
                    "start": int(start),
                    "end": int(end),
                    "quality": _first_value(md, "pct_success"),
                    "operational": _first_value(md, "pct_operational_error"),
                    "machine": _first_value(md, "pct_machine_error"),
                    "success": _first_value(md, "sum_success"),
                    "total": total,
                }
            )
        return out

    def fetch_asml_windows(
        self,
        station: str,
        windows: list[tuple[int, int]],
        *,
        timezone: str | None = None,
        dashboard_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ASML labeler quality/no-read/multi-read/misapplied per window.

        Each entry carries the window bounds, the four percentages and the raw
        ``total`` so callers can tell "no data / no manual labeler at this
        station" (total 0/None) apart from a real 0%.
        """
        station = (station or "").strip().upper()
        if not station:
            raise CwDashClientError("Falta el código de estación para ASML.")
        dash = dashboard_name or labeler_dashboard_name(station)
        bodies = [build_asml_body(station, s, e, timezone) for (s, e) in windows]
        if not bodies:
            return []
        batches = self.get_metric_data_batch(bodies, dashboard_name=dash)
        out: list[dict[str, Any]] = []
        for (start, end), md in zip(windows, batches):
            out.append(
                {
                    "start": int(start),
                    "end": int(end),
                    "quality": _first_value(md, "pct_quality"),
                    "noRead": _first_value(md, "pct_no_read"),
                    "multiRead": _first_value(md, "pct_multi_read"),
                    "misapplied": _first_value(md, "pct_misapplied"),
                    "total": _first_value(md, "sum_total"),
                }
            )
        return out

    def fetch_asl_windows(
        self,
        station: str,
        windows: list[tuple[int, int]],
        *,
        timezone: str | None = None,
        dashboard_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ASL quality/process/machine shares for each time window."""
        station = (station or "").strip().upper()
        if not station:
            raise CwDashClientError("Falta el código de estación para ASL.")
        dash = dashboard_name or asl_dashboard_name(station)
        bodies = [build_asl_body(station, s, e, timezone) for (s, e) in windows]
        if not bodies:
            return []
        batches = self.get_metric_data_batch(bodies, dashboard_name=dash)
        out: list[dict[str, Any]] = []
        for (start, end), md in zip(windows, batches):
            out.append(
                {
                    "start": int(start),
                    "end": int(end),
                    "quality": _first_value(md, "pct_quality"),
                    "process": _first_value(md, "pct_process"),
                    "machine": _first_value(md, "pct_machine"),
                    "total": _first_value(md, "sum_total"),
                }
            )
        return out

    def fetch_automation_windows(
        self,
        station: str,
        windows: list[tuple[int, int]],
        *,
        sources: Iterable[str] | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Return metrics for configured built-ins/custom dashboard names."""
        station = (station or "").strip().upper()
        descriptors = self.resolve_automation_sources(station, sources)
        fetchers = {
            "adta": self.fetch_adta_windows,
            "manual": self.fetch_asml_windows,
            "asl": self.fetch_asl_windows,
        }
        result_sources: dict[str, dict[str, Any]] = {}
        for descriptor in descriptors:
            source = str(descriptor.get("id") or "")
            profile = str(descriptor.get("profile") or "")
            dashboard = str(descriptor.get("dashboard") or "")
            rows: list[dict[str, Any]] = []
            error = str(descriptor.get("error") or "")
            if descriptor.get("available") and profile in fetchers:
                try:
                    rows = fetchers[profile](
                        station, windows, timezone=timezone, dashboard_name=dashboard,
                    )
                except CwDashAuthError:
                    raise
                except CwDashClientError as exc:
                    error = str(exc)
            result_sources[source] = {
                "dashboard": dashboard,
                "profile": profile,
                "available": bool(rows) and any(row.get("total") for row in rows),
                "rows": rows,
                "error": error,
            }

        failed = [info["error"] for info in result_sources.values() if info.get("error")]
        if result_sources and len(failed) == len(result_sources):
            raise CwDashClientError(failed[0])

        def _first_profile(profile: str) -> dict[str, Any]:
            return next(
                (info for info in result_sources.values() if info.get("profile") == profile),
                {},
            )

        manual = _first_profile("manual")
        return {
            "station": station,
            "timezone": _normalise_timezone(timezone),
            "sources": result_sources,
            "adta": _first_profile("adta").get("rows", []),
            "asml": manual.get("rows", []),
            "asml_available": bool(manual.get("available")),
        }


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


def fetch_adta_quality(
    station: str,
    windows: list[tuple[int, int]],
    *,
    cookies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the ADTA quality shares per time window for a station.

    Convenience wrapper around :meth:`CwDashboardsClient.fetch_adta_windows`
    so callers don't build a client themselves.
    """
    client = CwDashboardsClient(cookies=cookies)
    return client.fetch_adta_windows(station, windows)


def fetch_automation_quality(
    station: str,
    windows: list[tuple[int, int]],
    *,
    sources: Iterable[str] | None = None,
    timezone: str | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the selected automation metrics per window for a station."""
    client = CwDashboardsClient(cookies=cookies)
    return client.fetch_automation_windows(
        station, windows, sources=sources, timezone=timezone,
    )


def available_automation_sources(
    station: str,
    configured: Iterable[str] | None = None,
    *,
    cookies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve configured automation dashboards available at a station."""
    client = CwDashboardsClient(cookies=cookies)
    return client.available_automation_sources(station, configured)


__all__ = [
    "CwDashAuthError",
    "CwDashClientError",
    "CWDASH_LOGIN_URL",
    "CWDASH_BASE_URL",
    "CwDashboardsClient",
    "cookies_status",
    "sorter_dashboard_name",
    "labeler_dashboard_name",
    "asl_dashboard_name",
    "automation_dashboard_name",
    "available_automation_sources",
    "build_adta_body",
    "build_asml_body",
    "build_asl_body",
    "fetch_adta_quality",
    "fetch_automation_quality",
]
