"""Read-only TimeHub schedule client backed by A to Z Manager Portal APIs.

Forecast UTR must open the Manager Portal landing page, not an employee
record. The landing page does not mint the path-scoped QuickSight embed used
by the legacy client, so roster and shifts are read from the same AMEXService
endpoints used by ``managerView`` itself:

* ``POST /search-employees`` for the active station roster.
* ``POST /get-shifts`` for each employee's planned schedule.

Authentication follows A to Z's Midway OAuth flow and is cached in memory.
No A to Z write endpoint is exposed or called by this module.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, time as dt_time
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from app.integrations.firefox_cookies import midway_cookies_from_profile
from app.integrations.quicksight_client import QuickSightAuthError, QuickSightError


logger = logging.getLogger(__name__)


ATOZ_BASE = "https://atoz.amazon.work"
ATOZ_LOGIN_BASE = "https://atoz-login.amazon.work"
MANAGER_VIEW_URL = f"{ATOZ_BASE}/timecard/managerView"
_CLIENT_ID = "ATOZ_AMX_SERVICE"
_TIMECARD_OPERATION_URL = (
    f"{ATOZ_BASE}/apis/TimehubEverywhereMFE/service-api/TimeHubES/"
    "execute-timecard-operation/get-timecard-data"
)
_PAY_SUMMARY_COMPONENTS = (
    "AGGREGATED_PAYCODES",
    "TIMEAWAY",
    "REPORTED_ACTIVITY",
    "ANOMALIES",
)
_IDP_AUTHORIZE_URL = "https://idp.federate.amazon.com/api/oauth2/v1/authorize"
_OAUTH_CLIENT_ID = "atoz.web.prod.clientid"
_OAUTH_CALLBACK = f"{ATOZ_LOGIN_BASE}/federate_callback/{_OAUTH_CLIENT_ID}/"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
_SESSION_TTL_S = 20 * 60.0
_RESULT_TTL_S = 5 * 60.0
_MAX_WORKERS = 12

_session_lock = threading.Lock()
_session_cache: dict[str, Any] = {
    "cookies": {}, "csrf": "", "employee_id": "", "username": "", "at": 0.0,
}
_result_lock = threading.Lock()
_result_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_inflight: dict[tuple[str, str, str], threading.Event] = {}
_result_errors: dict[tuple[Any, ...], Exception] = {}


class AtoZTimehubAuthError(QuickSightAuthError):
    """A to Z/Midway authentication is unavailable."""


class AtoZTimehubError(QuickSightError):
    """A to Z accepted authentication but could not return schedule data."""


def _is_employee_access_error(error: Exception) -> bool:
    """Whether one employee is outside the operator's TimeHub permission scope.

    ``search-employees`` can include records that ``get-shifts`` refuses with
    this employee-level 400. It is not a session failure and must not discard
    the other authorized employees from the station roster.
    """
    return "AMEXService_Unauthorized_Access_Error" in str(error)


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if value)


def _response_location(response: requests.Response) -> str:
    return urljoin(response.url, response.headers.get("Location", ""))


def _intermediate_url(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, str) and payload.startswith("http"):
            return payload
    except (ValueError, requests.JSONDecodeError):
        pass
    text = response.text.strip().strip('"')
    if text.startswith("http"):
        return text
    return _response_location(response)


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def _authenticate(timeout: float = 30.0) -> dict[str, Any]:
    midway = midway_cookies_from_profile()
    if not midway:
        raise AtoZTimehubAuthError(
            "No hay sesion Midway disponible en Firefox para autenticar A to Z."
        )

    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": _USER_AGENT})

    login_url = (
        f"{ATOZ_LOGIN_BASE}/login?returnUrl="
        "https%3A%2F%2Fatoz.amazon.work%2Ftimecard%2FmanagerView"
    )
    login_response = session.get(login_url, timeout=timeout, allow_redirects=False)
    if login_response.status_code >= 500:
        raise AtoZTimehubError(f"A to Z login devolvio HTTP {login_response.status_code}")

    state = uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    auth_session = base64.b64encode(
        json.dumps({"federateState": state, "federateNonce": nonce}).encode("utf-8")
    ).decode("ascii")
    session.cookies.set("_auth_session", auth_session, domain=".amazon.work", path="/")

    authorize_url = f"{_IDP_AUTHORIZE_URL}?{urlencode({
        'client_id': _OAUTH_CLIENT_ID,
        'redirect_uri': _OAUTH_CALLBACK,
        'scope': 'openid',
        'response_type': 'code',
        'state': state,
        'nonce': nonce,
        'login_hint': 'corp',
        'response_mode': 'query',
    })}"
    authorize = session.get(authorize_url, timeout=timeout, allow_redirects=False)
    midway_redirect = _response_location(authorize)
    if "midway-auth.amazon.com" not in midway_redirect:
        raise AtoZTimehubAuthError("A to Z no redirigio la autenticacion a Midway.")

    midway_session = requests.Session()
    midway_session.trust_env = False
    midway_response = midway_session.get(
        midway_redirect,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Redirect-Location": "true",
            "User-Agent": _USER_AGENT,
            "Cookie": _cookie_header(midway),
        },
        timeout=timeout,
        allow_redirects=False,
    )
    intermediate = _intermediate_url(midway_response)
    if not intermediate.startswith("http"):
        raise AtoZTimehubAuthError("Midway no devolvio la URL intermedia de A to Z.")

    callback_response = session.get(intermediate, timeout=timeout, allow_redirects=False)
    callback_url = _response_location(callback_response)
    parsed_callback = urlparse(callback_url)
    callback_query = parse_qs(parsed_callback.query)
    if not callback_query.get("code"):
        raise AtoZTimehubAuthError("Midway no devolvio el codigo OAuth de A to Z.")
    if callback_query.get("state", [""])[0] != state:
        raise AtoZTimehubAuthError("El estado OAuth de A to Z no coincide.")

    exchange = session.get(callback_url, timeout=timeout, allow_redirects=False)
    redirect = _response_location(exchange)
    if redirect and "atoz.amazon.work" in redirect:
        session.get(redirect, timeout=timeout, allow_redirects=False)

    initialize = session.get(
        f"{ATOZ_LOGIN_BASE}/initialize",
        headers={
            "anti-csrftoken-a2z-request": "true",
            "Origin": ATOZ_BASE,
            "Referer": f"{ATOZ_BASE}/",
        },
        timeout=timeout,
        allow_redirects=False,
    )
    if initialize.status_code in (401, 403) or initialize.status_code >= 500:
        raise AtoZTimehubAuthError(
            f"A to Z no pudo inicializar la sesion (HTTP {initialize.status_code})."
        )

    cookies = {cookie.name: cookie.value for cookie in session.cookies if cookie.value}
    token = cookies.get("atoz-id-token") or cookies.get("atoz-oauth-token") or ""
    claims = _jwt_payload(token)
    employee_id = str(claims.get("EMPLOYEE_NUMBER") or claims.get("sub") or "")
    username = str(
        claims.get("cloudauth:username") or claims.get("preferred_username") or ""
    ).split("@")[0]
    if not cookies or not employee_id:
        raise AtoZTimehubAuthError("A to Z no devolvio una sesion de operador valida.")

    return {
        "cookies": cookies,
        "csrf": initialize.headers.get("anti-csrftoken-a2z", ""),
        "employee_id": employee_id,
        "username": username,
        "at": time.time(),
    }


def _get_session(*, force: bool = False) -> dict[str, Any]:
    with _session_lock:
        age = time.time() - float(_session_cache.get("at") or 0)
        if not force and _session_cache.get("cookies") and age < _SESSION_TTL_S:
            return dict(_session_cache)
        fresh = _authenticate()
        _session_cache.update(fresh)
        return dict(_session_cache)


def invalidate_session() -> None:
    with _session_lock:
        _session_cache.update(
            cookies={}, csrf="", employee_id="", username="", at=0.0
        )


def _is_login_response(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    text = response.text[:1000] if "json" not in content_type else ""
    return (
        response.status_code == 401
        or "atoz-login.amazon.work" in str(response.url)
        or "Amazon A to Z Login" in text
    )


def _api_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    referer: str = MANAGER_VIEW_URL,
    timeout: float = 45.0,
) -> dict[str, Any]:
    auth = _get_session()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": ATOZ_BASE,
        "Referer": referer,
        "User-Agent": _USER_AGENT,
        "Cookie": _cookie_header(auth["cookies"]),
    }
    if auth.get("csrf"):
        headers["anti-csrftoken-a2z"] = str(auth["csrf"])
    api_session = requests.Session()
    api_session.trust_env = False

    # Manager Portal rate-limits the per-employee get-shifts fan-out. A
    # throttled read is safe to retry: respect Retry-After when present and
    # otherwise use a short exponential backoff with per-worker jitter so the
    # same batch does not immediately collide again. Transient upstream 5xx
    # responses follow the same bounded retry contract.
    max_attempts = 4
    response: requests.Response | None = None
    for attempt in range(max_attempts):
        response = api_session.request(
            method,
            f"{ATOZ_BASE}/apis/AMEXService/{path}",
            params={"clientId": _CLIENT_ID},
            json=payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        if _is_login_response(response):
            raise AtoZTimehubAuthError("La sesion de A to Z ha caducado.")

        transient = response.status_code == 429 or response.status_code >= 500
        if transient and attempt < max_attempts - 1:
            try:
                retry_after = float(response.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            base_delay = retry_after if retry_after > 0 else 0.75 * (2 ** attempt)
            jitter = (threading.get_ident() % 7) * 0.05
            time.sleep(min(5.0, max(0.25, base_delay)) + jitter)
            continue
        break

    if response is None:  # pragma: no cover - loop always executes
        raise AtoZTimehubError(f"AMEXService {path} no realizo ninguna peticion.")
    if response.status_code >= 400:
        raise AtoZTimehubError(
            f"AMEXService {path} devolvio HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise AtoZTimehubError(f"AMEXService {path} no devolvio JSON.") from exc
    if not isinstance(data, dict):
        raise AtoZTimehubError(f"AMEXService {path} devolvio una respuesta inesperada.")
    return data


def _search_employees(station: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
    employees: list[dict[str, Any]] = []
    page = 1
    while True:
        result = _api_json(
            "POST",
            "search-employees",
            payload={
                "pageNumber": page,
                "pageSize": 2000,
                "filters": [{
                    "scope": "ALL",
                    "managersOnly": False,
                    "onPremise": False,
                    "sites": [station],
                    "costCenterIds": [],
                    "employeeStatuses": ["A"],
                    "jobLevels": [],
                    "employmentTypes": [],
                    "shiftCohorts": [],
                    "badgeColors": [],
                    "lastHireDate": None,
                    "employeeSearch": None,
                    "employeePartialSearch": None,
                    "startDateTime": date_from,
                    "endDateTime": date_to,
                    "countries": [],
                    "managerIds": [],
                }],
                "sort": "employeeId",
                "order": "desc",
                "permission": "WorkEvents ManagerPortal ops experience",
            },
        )
        body = result.get("data") if isinstance(result.get("data"), dict) else result
        batch = body.get("employees") if isinstance(body, dict) else []
        if isinstance(batch, list):
            employees.extend(item for item in batch if isinstance(item, dict))
        total_pages = int(body.get("totalPages") or 1) if isinstance(body, dict) else 1
        if page >= total_pages:
            break
        page += 1
    return employees


def _default_timezone(country: str) -> str:
    return {
        "ESP": "Europe/Madrid", "ES": "Europe/Madrid",
        "FRA": "Europe/Paris", "FR": "Europe/Paris",
        "DEU": "Europe/Berlin", "DE": "Europe/Berlin",
        "ITA": "Europe/Rome", "IT": "Europe/Rome",
        "GBR": "Europe/London", "GB": "Europe/London",
    }.get(country.upper(), "Europe/Madrid")


def _boundary(date_iso: str, timezone: str, *, end: bool) -> str:
    zone = ZoneInfo(timezone)
    value = datetime.combine(
        date.fromisoformat(date_iso), dt_time.max if end else dt_time.min, tzinfo=zone
    )
    return value.isoformat(timespec="milliseconds")


def _timestamp(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("timestamp") or "")
    return str(item or "")


def _employee_shifts(
    employee: dict[str, Any], date_from: str, date_to: str, *, timeout: float = 45.0,
) -> list[dict[str, Any]]:
    employee_id = str(employee.get("id") or "").strip()
    login = str(employee.get("login") or "").strip()
    if not employee_id or not login:
        return []
    country = str(employee.get("country") or "ESP").strip().upper()
    timezone = str(employee.get("timeZone") or "").strip() or _default_timezone(country)
    result = _api_json(
        "POST",
        "get-shifts/",
        payload={
            "concernedEmployeeId": employee_id,
            "startDate": {"timestamp": _boundary(date_from, timezone, end=False), "timezone": timezone},
            "endDate": {"timestamp": _boundary(date_to, timezone, end=True), "timezone": timezone},
            "userExperience": "NEW_UX_EXPERIENCE",
            "concernedEmployeeCountryCode": country,
        },
        referer=f"{MANAGER_VIEW_URL}/employeeDetails/{login}",
        timeout=timeout,
    )
    body = result.get("data") if isinstance(result.get("data"), dict) else result
    shifts = body.get("shifts") if isinstance(body, dict) else []
    rows: list[dict[str, Any]] = []
    for shift in shifts if isinstance(shifts, list) else []:
        intervals = shift.get("intervals") if isinstance(shift, dict) else []
        if not isinstance(intervals, list):
            continue
        starts = [_timestamp(item.get("startDateTime")) for item in intervals if isinstance(item, dict)]
        ends = [_timestamp(item.get("endDateTime")) for item in intervals if isinstance(item, dict)]
        starts = sorted(value for value in starts if value)
        ends = sorted(value for value in ends if value)
        if not starts or not ends:
            continue
        logical_day = starts[0][:10]
        # get-shifts returns every shift overlapping the requested range. Keep
        # only shifts whose actual start belongs to the selected week.
        if logical_day < date_from or logical_day > date_to:
            continue
        rows.append({
            "employee_id": employee_id,
            "first_name": str(employee.get("firstName") or "").strip(),
            "last_name": str(employee.get("lastName") or "").strip(),
            "login": login,
            "name": " ".join(
                part for part in (str(employee.get("firstName") or "").strip(),
                                  str(employee.get("lastName") or "").strip()) if part
            ),
            "manager": str(employee.get("managerLogin") or "").strip(),
            "manager_name": " ".join(
                part for part in (str(employee.get("managerFirstName") or "").strip(),
                                  str(employee.get("managerLastName") or "").strip()) if part
            ),
            "pattern": str(employee.get("shiftCohort") or "").strip(),
            "status": str(employee.get("employeeStatus") or "").strip(),
            "type": str(employee.get("employeeType") or "").strip(),
            # TimeHub's employee search uses BLUE / YELLOW / GREEN. Keep the
            # source token stable in the API; Forecast UTR groups YELLOW and
            # GREEN as Green Badge to mirror the original Labor Planner.
            "badge_color": str(employee.get("badgeColor") or "UNKNOWN").strip().upper(),
            "date": logical_day,
            "shift_start": starts[0][:16],
            "shift_end": ends[-1][:16],
        })
    return rows


def _fetch_shift_roster_once(
    *,
    site: str,
    date_from: str,
    date_to: str,
    timeout: float,
) -> dict[str, Any]:
    employees = _search_employees(site, date_from, date_to)
    rows: list[dict[str, Any]] = []
    errors: list[Exception] = []
    unauthorized_employees: list[dict[str, Any]] = []
    auth_error: AtoZTimehubAuthError | None = None
    executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    futures = {
        executor.submit(
            _employee_shifts,
            employee,
            date_from,
            date_to,
            timeout=min(45.0, max(5.0, timeout)),
        ): employee
        for employee in employees
    }
    try:
        done, pending = wait(futures, timeout=max(1.0, timeout))
        for future in done:
            try:
                rows.extend(future.result())
            except AtoZTimehubAuthError as exc:
                auth_error = auth_error or exc
            except Exception as exc:  # noqa: BLE001 - classify per employee
                employee = futures[future]
                if _is_employee_access_error(exc):
                    unauthorized_employees.append(employee)
                else:
                    errors.append(exc)
        for future in pending:
            future.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if auth_error is not None:
        raise auth_error
    if pending:
        raise AtoZTimehubError(
            f"TimeHub no completo {len(pending)} de {len(futures)} consultas dentro del tiempo limite."
        )
    if errors:
        raise AtoZTimehubError(
            f"TimeHub fallo en {len(errors)} de {len(futures)} consultas; se descarta el roster parcial."
        )

    deduped = {
        (row["login"], row["shift_start"], row["shift_end"]): row for row in rows
    }
    ordered = sorted(deduped.values(), key=lambda row: (row["date"], row["login"], row["shift_start"]))
    unauthorized_ids = {
        str(employee.get("id") or "").strip()
        for employee in unauthorized_employees
    }
    if unauthorized_ids:
        logger.warning(
            "TimeHub omitted %d employee records outside the operator permission scope",
            len(unauthorized_ids),
        )
    people = []
    for employee in employees:
        if str(employee.get("id") or "").strip() in unauthorized_ids:
            continue
        login = str(employee.get("login") or "").strip()
        if not login:
            continue
        people.append({
            "employee_id": str(employee.get("id") or "").strip(),
            "login": login,
            "name": " ".join(
                part for part in (
                    str(employee.get("firstName") or "").strip(),
                    str(employee.get("lastName") or "").strip(),
                ) if part
            ),
            "manager": str(employee.get("managerLogin") or "").strip(),
            "manager_name": " ".join(
                part for part in (
                    str(employee.get("managerFirstName") or "").strip(),
                    str(employee.get("managerLastName") or "").strip(),
                ) if part
            ),
            "pattern": str(employee.get("shiftCohort") or "").strip(),
            "status": str(employee.get("employeeStatus") or "").strip(),
            "type": str(employee.get("employeeType") or "").strip(),
            "badge_color": str(employee.get("badgeColor") or "UNKNOWN").strip().upper(),
        })
    people.sort(key=lambda person: (person["name"], person["login"]))
    return {
        "site": site,
        "date_from": date_from,
        "date_to": date_to,
        "rows": ordered,
        "people": people,
        "source": "atoz-manager-view",
        "skipped_unauthorized": len(unauthorized_ids),
    }


def fetch_shift_roster(
    *,
    site: str,
    date_from: str,
    date_to: str,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Return station roster shifts from the Manager Portal read APIs."""
    site = str(site or "").strip().upper()
    if not site:
        raise AtoZTimehubError("Falta la estacion para consultar TimeHub.")
    key = (site, date_from, date_to)
    deadline = time.monotonic() + max(1.0, timeout)

    with _result_lock:
        cached = _result_cache.get(key)
        if cached and time.time() - cached[0] < _RESULT_TTL_S:
            return dict(cached[1])
        event = _inflight.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _inflight[key] = event
            _result_errors.pop(key, None)

    if not owner:
        if not event.wait(timeout=max(1.0, deadline - time.monotonic())):
            raise AtoZTimehubError("Otra extraccion de TimeHub no termino dentro del tiempo limite.")
        with _result_lock:
            cached = _result_cache.get(key)
            error = _result_errors.get(key)
        if cached and time.time() - cached[0] < _RESULT_TTL_S:
            return dict(cached[1])
        if error is not None:
            raise error
        raise AtoZTimehubError("La extraccion compartida de TimeHub no devolvio resultado.")

    try:
        remaining = max(1.0, deadline - time.monotonic())
        try:
            result = _fetch_shift_roster_once(
                site=site,
                date_from=date_from,
                date_to=date_to,
                timeout=remaining,
            )
        except AtoZTimehubAuthError:
            invalidate_session()
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                raise AtoZTimehubError("TimeHub agoto el tiempo al renovar la sesion.")
            result = _fetch_shift_roster_once(
                site=site,
                date_from=date_from,
                date_to=date_to,
                timeout=remaining,
            )
        with _result_lock:
            _result_cache[key] = (time.time(), result)
            _result_errors.pop(key, None)
        return dict(result)
    except Exception as exc:
        with _result_lock:
            _result_errors[key] = exc
        raise
    finally:
        with _result_lock:
            completed = _inflight.pop(key, event)
            completed.set()


def _flatten_pay_item(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten one TimeHub item into stable table columns."""
    if not isinstance(value, dict):
        return {prefix or "value": value}
    out: dict[str, Any] = {}
    for key, item in value.items():
        column = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            out.update(_flatten_pay_item(item, column))
        elif isinstance(item, list):
            out[column] = json.dumps(item, ensure_ascii=False, default=str)
        else:
            out[column] = item
    return out


def _employee_pay_summary(
    employee: dict[str, Any], date_from: str, date_to: str, *, timeout: float,
) -> dict[str, list[dict[str, Any]]]:
    employee_id = str(employee.get("id") or "").strip()
    login = str(employee.get("login") or "").strip()
    if not employee_id or not login:
        return {}
    timezone = str(employee.get("timeZone") or "").strip() or _default_timezone(
        str(employee.get("country") or "ESP")
    )
    operation_payload = {
        "concernedEmployeeId": employee_id,
        "concernedEmployeeTimezone": timezone,
        "startDate": date_from,
        "endDate": date_to,
        "components": list(_PAY_SUMMARY_COMPONENTS),
    }
    body = {
        "payload": json.dumps(operation_payload, separators=(",", ":")),
        "operationType": "GET_TIMECARD_DATA",
        "concernedEmployeeId": employee_id,
    }
    auth = _get_session()
    response = requests.post(
        _TIMECARD_OPERATION_URL,
        params={"clientId": _CLIENT_ID},
        json=body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": ATOZ_BASE,
            "Referer": f"{MANAGER_VIEW_URL}/employeeDetails/{login}",
            "User-Agent": _USER_AGENT,
            "Cookie": _cookie_header(auth["cookies"]),
            **(
                {"anti-csrftoken-a2z": str(auth["csrf"])}
                if auth.get("csrf") else {}
            ),
        },
        timeout=timeout,
        allow_redirects=True,
    )
    if _is_login_response(response) or response.status_code in (401, 403):
        raise AtoZTimehubAuthError("La sesion de A to Z ha caducado.")
    if response.status_code >= 400:
        raise AtoZTimehubError(
            f"TimeHub pay summary devolvio HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )
    try:
        envelope = response.json()
        raw = envelope.get("response") if isinstance(envelope, dict) else None
        payload = json.loads(raw) if isinstance(raw, str) else envelope
    except (ValueError, json.JSONDecodeError) as exc:
        raise AtoZTimehubError("TimeHub devolvio Pay Summary ilegible.") from exc
    if not isinstance(payload, dict):
        raise AtoZTimehubError("TimeHub devolvio Pay Summary con formato inesperado.")

    name = " ".join(
        part for part in (
            str(employee.get("firstName") or "").strip(),
            str(employee.get("lastName") or "").strip(),
        ) if part
    )
    categories = {
        "pay_codes": "payCodes",
        "time_off": "timeOff",
        "reported_activities": "reportedActivities",
        "anomalies": "anomalies",
    }
    out: dict[str, list[dict[str, Any]]] = {key: [] for key in categories}
    days = payload.get("timeCardDayList")
    for day in days if isinstance(days, list) else []:
        if not isinstance(day, dict):
            continue
        day_iso = str(day.get("date") or "")[:10]
        for category, source_key in categories.items():
            items = day.get(source_key)
            for item in items if isinstance(items, list) else []:
                row = {
                    "employee_id": employee_id,
                    "employee_first_name": str(employee.get("firstName") or "").strip(),
                    "employee_last_name": str(employee.get("lastName") or "").strip(),
                    "employee_pattern": str(employee.get("shiftCohort") or "").strip(),
                    "employee_login": login,
                    "employee_name": name,
                    "manager_login": str(employee.get("managerLogin") or "").strip(),
                    "date": day_iso,
                }
                row.update(_flatten_pay_item(item))
                out[category].append(row)
    return out


def fetch_pay_summary(
    *,
    site: str,
    date_from: str,
    date_to: str,
    timeout: float = 90.0,
    manager_logins: tuple[str, ...] = (),
    excluded_logins: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return scoped Reports -> Pay Summary via A to Z Manager Portal."""
    site = str(site or "").strip().upper()
    if not site:
        raise AtoZTimehubError("Falta la estacion para consultar Pay Summary.")
    allowed_managers = {
        str(login or "").strip().lower() for login in manager_logins if str(login or "").strip()
    }
    excluded = {
        str(login or "").strip().lower() for login in excluded_logins if str(login or "").strip()
    }
    try:
        employee_pool = _search_employees(site, date_from, date_to)
    except AtoZTimehubAuthError:
        # A server-rejected session must not stay "ready" in the shared
        # status probe for the full cache TTL. Clearing it lets Reconectar
        # either mint a fresh session or open the A to Z deep link.
        invalidate_session()
        raise
    employees = [
        employee
        for employee in employee_pool
        if str(employee.get("managerLogin") or "").strip().lower() in allowed_managers
        and str(employee.get("login") or "").strip().lower() not in excluded
    ]
    grouped: dict[str, list[dict[str, Any]]] = {
        "pay_codes": [], "time_off": [], "reported_activities": [], "anomalies": [],
    }
    errors: list[Exception] = []
    auth_error: AtoZTimehubAuthError | None = None
    executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    futures = {
        executor.submit(
            _employee_pay_summary,
            employee,
            date_from,
            date_to,
            timeout=min(45.0, max(5.0, timeout)),
        ): employee
        for employee in employees
    }
    try:
        done, pending = wait(futures, timeout=max(1.0, timeout))
        for future in done:
            try:
                result = future.result()
                for category, rows in result.items():
                    grouped[category].extend(rows)
            except AtoZTimehubAuthError as exc:
                auth_error = auth_error or exc
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        for future in pending:
            future.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if auth_error is not None:
        invalidate_session()
        raise auth_error
    if pending:
        errors.append(AtoZTimehubError(
            f"TimeHub no completo {len(pending)} consultas de Pay Summary."
        ))
    if errors and len(errors) >= len(futures):
        raise AtoZTimehubError(
            "TimeHub no pudo completar ninguna consulta de Pay Summary."
        )

    def clean_token(value: Any) -> str:
        text = str(value or "").strip()
        for prefix in ("ManualPayCode_ESP-", "TimeOff_ESP_", "ESP-"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return " ".join(text.replace("_", " ").replace("-", " ").split()).title()

    def hours(amount: Any, unit: Any) -> float | None:
        try:
            value = float(amount)
        except (TypeError, ValueError):
            return None
        normalized_unit = str(unit or "").strip().lower()
        if normalized_unit in {"second", "seconds"}:
            value /= 3600.0
        elif normalized_unit in {"minute", "minutes"}:
            value /= 60.0
        return round(value, 2)

    def short_datetime(value: Any) -> str:
        text = str(value or "").strip()
        return text[:16].replace("T", " ") if text else ""

    column_keys = {
        "pay_codes": ("employee_login", "employee_name", "manager_login", "date", "concept", "hours"),
        "time_off": ("employee_login", "employee_name", "manager_login", "date", "type", "status", "hours", "start", "end", "origin"),
        "reported_activities": ("employee_login", "employee_name", "manager_login", "date", "type", "status", "hours", "start", "end"),
        "anomalies": ("employee_login", "employee_name", "manager_login", "date", "type", "status", "description", "resolution", "detected", "updated"),
    }
    labels = {
        "pay_codes": "Pay Codes",
        "time_off": "Time Away",
        "reported_activities": "Reported Activities",
        "anomalies": "Anomalies",
    }
    normalized_rows: dict[str, list[dict[str, Any]]] = {key: [] for key in labels}
    for category, raw_rows in grouped.items():
        for raw in raw_rows:
            base = {
                "employee_id": raw.get("employee_id") or "",
                "employee_first_name": raw.get("employee_first_name") or "",
                "employee_last_name": raw.get("employee_last_name") or "",
                "employee_pattern": raw.get("employee_pattern") or "",
                "employee_login": raw.get("employee_login") or "",
                "employee_name": raw.get("employee_name") or "",
                "manager_login": raw.get("manager_login") or "",
                "date": str(raw.get("date") or raw.get("logicalDay") or "")[:10],
            }
            if category == "pay_codes":
                base.update(
                    concept=clean_token(raw.get("mtPayCodeName") or raw.get("salaryCodeName")),
                    hours=hours(raw.get("amount"), raw.get("unit")),
                )
            elif category == "time_off":
                base.update(
                    type=clean_token(raw.get("timeOffType")),
                    status=clean_token(raw.get("status")),
                    hours=hours(raw.get("duration.amount"), raw.get("duration.unit")),
                    start=short_datetime(raw.get("startDateTime.timestamp")),
                    end=short_datetime(raw.get("endDateTime.timestamp")),
                    origin=clean_token(raw.get("origin")),
                )
            elif category == "anomalies":
                base.update(
                    type=clean_token(raw.get("deviationName")),
                    status=clean_token(raw.get("deviationStatus")),
                    description=str(raw.get("description") or "").strip(),
                    resolution=str(raw.get("resolution") or "").strip(),
                    detected=short_datetime(raw.get("detectionDate")),
                    updated=short_datetime(raw.get("lastModifiedDatetime")),
                )
            else:
                base.update(
                    type=clean_token(
                        raw.get("reportedActivityName")
                        or raw.get("activityName")
                        or raw.get("type")
                    ),
                    status=clean_token(raw.get("status")),
                    hours=hours(
                        raw.get("duration.amount") or raw.get("amount"),
                        raw.get("duration.unit") or raw.get("unit"),
                    ),
                    start=short_datetime(
                        raw.get("startDateTime.timestamp") or raw.get("startDateTime")
                    ),
                    end=short_datetime(
                        raw.get("endDateTime.timestamp") or raw.get("endDateTime")
                    ),
                )
            normalized_rows[category].append(base)

    selected_sections = (
        (
            "overtime_comp",
            "Not Approved Overtime & Comp Hours",
            [
                row for row in normalized_rows["pay_codes"]
                if (
                    "not approved overtime" in str(row.get("concept") or "").lower()
                    or "comp hours" in str(row.get("concept") or "").lower()
                )
            ],
            column_keys["pay_codes"],
        ),
        (
            "absence",
            "Absence",
            [
                row for row in normalized_rows["time_off"]
                if "absence" in str(row.get("type") or "").lower()
            ],
            column_keys["time_off"],
        ),
    )
    sections: list[dict[str, Any]] = []
    for section_key, title, selected_rows, selected_columns in selected_sections:
        rows = sorted(
            selected_rows,
            key=lambda row: (str(row.get("date") or ""), str(row.get("employee_login") or "")),
        )
        sections.append({
            "sheet": "Reports",
            "title": title,
            "visual_id": f"atoz-pay-summary-{section_key}",
            "columns": [{"key": key, "label": key} for key in selected_columns],
            "rows": rows,
            "available": len(rows),
            "complete": not errors,
        })
    return {
        "site": site,
        "date_from": date_from,
        "date_to": date_to,
        "report": "Pay Summary",
        "sections": sections,
        "errors": ([{
            "title": "Pay Summary",
            "visual_id": "atoz-pay-summary-partial",
            "message": f"{len(errors)} employee queries failed.",
        }] if errors else []),
        "source": "atoz-manager-view",
    }


def fetch_flex_balances(
    *, site: str, date_from: str, date_to: str, logins: list[str], timeout: float = 60.0,
) -> dict[str, Any]:
    """Return positive/negative Flex balances from A to Z in hours.

    ``BulkFetchBalancesRequest.employeeIdsList`` accepts exactly one employee
    ID. Requests therefore run independently with bounded concurrency so one
    inaccessible employee does not discard valid balances for the others.
    """
    wanted = {str(login).strip().lower() for login in logins if str(login).strip()}
    employees = [
        employee for employee in _search_employees(site, date_from, date_to)
        if str(employee.get("login") or "").strip().lower() in wanted
    ]

    requests_by_employee: list[tuple[dict[str, Any], str, str, str]] = []
    for employee in employees:
        employee_id = str(employee.get("id") or "").strip()
        login = str(employee.get("login") or "").strip().lower()
        if not employee_id or not login:
            continue
        country = str(employee.get("country") or "ESP").strip().upper()
        zone = str(employee.get("timeZone") or "").strip() or _default_timezone(country)
        requests_by_employee.append((employee, employee_id, login, zone))

    def _fetch_one(employee_id: str, zone: str) -> dict[str, Any]:
        return _api_json(
            "POST", "bulk-fetch-balances",
            payload={
                "targetDateTime": _boundary(date_to, zone, end=True),
                "targetDateTimezone": zone,
                "employeeIdsList": [employee_id],
                "userExperience": "NEW_UX_EXPERIENCE",
            },
            timeout=min(45.0, max(5.0, timeout)),
        )

    rows: list[dict[str, Any]] = []
    matched: set[str] = set()
    errors: list[dict[str, str]] = []
    auth_error: AtoZTimehubAuthError | None = None
    executor = ThreadPoolExecutor(max_workers=min(6, max(1, len(requests_by_employee))))
    futures = {
        executor.submit(_fetch_one, employee_id, zone): (employee_id, login)
        for _employee, employee_id, login, zone in requests_by_employee
    }
    try:
        done, pending = wait(futures, timeout=max(1.0, timeout))
        for future in done:
            employee_id, login = futures[future]
            try:
                result = future.result()
                body = result.get("data") if isinstance(result.get("data"), dict) else result
                mapping = body.get("employeeIdToBalances") if isinstance(body, dict) else {}
                if not isinstance(mapping, dict):
                    raise AtoZTimehubError("TimeHub devolvio balances con formato inesperado.")
                encoded = mapping.get(employee_id)
                if encoded is None and len(mapping) == 1:
                    encoded = next(iter(mapping.values()))
                cell = {"login": login, "positive_flex": None, "negative_flex": None}
                entries = encoded if isinstance(encoded, list) else [encoded]
                for entry in entries:
                    if isinstance(entry, str):
                        entry = json.loads(entry)
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("balanceName") or "").strip()
                    normalized = " ".join(name.casefold().replace("_", " ").split())
                    if "flex positive" in normalized or "positive flex" in normalized:
                        field = "positive_flex"
                    elif "flex negative" in normalized or "negative flex" in normalized:
                        field = "negative_flex"
                    else:
                        continue
                    unit = str(entry.get("balanceUnit") or "").strip().lower()

                    def _hours(raw: object) -> float | None:
                        try:
                            numeric = float(raw)
                        except (TypeError, ValueError):
                            return None
                        if unit in ("minute", "minutes"):
                            return numeric / 60.0
                        if unit in ("hour", "hours"):
                            return numeric
                        return None

                    current_hours = _hours(entry.get("currentBalance"))
                    if current_hours is None:
                        continue
                    available_hours = _hours(entry.get("availableBalance"))
                    cell[field] = current_hours
                    cell[f"{field}_available"] = available_hours
                    cell[f"{field}_detail"] = {
                        "balance_name": name,
                        "current_balance": entry.get("currentBalance"),
                        "available_balance": entry.get("availableBalance"),
                        "source_unit": unit,
                        "hours_per_day": entry.get("hoursPerDay"),
                        "hours": current_hours,
                        "available_hours": available_hours,
                        "unit": "hours",
                    }
                    matched.add(name)
                if cell["positive_flex"] is not None or cell["negative_flex"] is not None:
                    rows.append(cell)
            except AtoZTimehubAuthError as exc:
                auth_error = auth_error or exc
            except Exception as exc:  # noqa: BLE001 - preserve other employees
                errors.append({
                    "employee_id": employee_id,
                    "login": login,
                    "message": str(exc),
                })
        for future in pending:
            employee_id, login = futures[future]
            future.cancel()
            errors.append({
                "employee_id": employee_id,
                "login": login,
                "message": "timeout",
            })
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if auth_error is not None:
        invalidate_session()
        raise auth_error
    return {
        "site": site, "date_from": date_from, "date_to": date_to,
        "rows": sorted(rows, key=lambda row: row["login"]),
        "matched_columns": sorted(matched), "errors": errors,
    }


def fetch_approved_vacation_dates(
    *, site: str, date_from: str, date_to: str, logins: list[str], timeout: float = 60.0,
) -> dict[str, set[str]]:
    """Approved Spanish vacation dates by login (read-only)."""
    wanted = {str(login).strip().lower() for login in logins if str(login).strip()}
    employees = [e for e in _search_employees(site, date_from, date_to)
                 if str(e.get("login") or "").strip().lower() in wanted]
    id_to_login = {str(e.get("id")): str(e.get("login")).strip().lower()
                   for e in employees if e.get("id") and e.get("login")}
    out: dict[str, set[str]] = {login: set() for login in wanted}
    vacation_types = {"TimeOff_ESP_Vacation", "ManualPayCode_ESP-Vacation-Day-CPC"}
    for offset in range(0, len(id_to_login), 100):
        ids = list(id_to_login)[offset:offset + 100]
        page = 1
        while True:
            result = _api_json("POST", "search-time-off-requests", payload={
                "employeeFilters": {"scope":"ALL","managersOnly":False,"onPremise":False,
                    "sites":[site],"costCenterIds":[],"employeeStatuses":["A"],"jobLevels":[],
                    "employmentTypes":[],"shiftCohorts":[],"badgeColors":[],"lastHireDate":None,
                    "employeeSearch":None,"employeePartialSearch":None,"startDateTime":date_from,
                    "endDateTime":date_to,"countries":[],"managerIds":[]},
                "timeOffFilters": {"employeeIds":ids,"statuses":["approved"],
                    "startDateTime":{"timestamp":_boundary(date_from,"Europe/Madrid",end=False),"timezone":"Europe/Madrid"},
                    "endDateTime":{"timestamp":_boundary(date_to,"Europe/Madrid",end=True),"timezone":"Europe/Madrid"}},
                "sortCriteria":{"sortFields":[{"field":"sid","direction":"ASC"}]},
                "maxResults":100,"pageNumber":page,"aggregatedAttributeNameList":["TIMEOFF_TYPES"]}, timeout=timeout)
            rows = result.get("timeOffRequestList") or []
            for item in rows:
                if not isinstance(item, dict) or item.get("status") != "approved" or item.get("timeOffType") not in vacation_types:
                    continue
                login = id_to_login.get(str(item.get("employeeId") or ""))
                start = _timestamp(item.get("startDateTime"))[:10]
                end = _timestamp(item.get("endDateTime"))[:10]
                if not login or not start or not end:
                    continue
                current = date.fromisoformat(start)
                last = date.fromisoformat(end)
                while current <= last:
                    out[login].add(current.isoformat())
                    current = date.fromordinal(current.toordinal() + 1)
            total = int(result.get("totalCount") or 0)
            if page * 100 >= total or len(rows) < 100:
                break
            page += 1
    return out


def timehub_login_status(timeout: float = 20.0) -> dict[str, Any]:
    """Whether A to Z Manager Portal APIs can be authenticated read-only."""
    del timeout
    try:
        auth = _get_session()
        return {
            "ready": bool(auth.get("cookies") and auth.get("employee_id")),
            "login_url": MANAGER_VIEW_URL,
            "host": "atoz.amazon.work",
            "source": "atoz-manager-view",
        }
    except (AtoZTimehubAuthError, AtoZTimehubError) as exc:
        return {
            "ready": False,
            "login_url": MANAGER_VIEW_URL,
            "host": "atoz.amazon.work",
            "source": "atoz-manager-view",
            "reason": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ready": False,
            "login_url": MANAGER_VIEW_URL,
            "host": "atoz.amazon.work",
            "source": "atoz-manager-view",
            "reason": f"error: {exc}",
        }


__all__ = [
    "AtoZTimehubAuthError",
    "AtoZTimehubError",
    "MANAGER_VIEW_URL",
    "fetch_flex_balances",
    "fetch_pay_summary",
    "fetch_approved_vacation_dates",
    "fetch_shift_roster",
    "invalidate_session",
    "timehub_login_status",
]
