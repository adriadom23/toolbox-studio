"""CSP Station Editor (EUCO - Shift Inputs Platform) roster-check client.

Reads the "Scheduled HC" roster the Station Editor shows on ``/inputs`` so
the toolbox can list, for the next operational day (D+1), every scheduled
employee id together with its MyTime status — without opening a browser.

Pure HTTP, no Selenium, no browser automation. Auth reuses the session the
operator already holds in Firefox:

* ``federate_id_token-0`` + ``AWSALBAuthNonce`` — read from ``cookies.sqlite``.
* ``session`` (Flask, session-scoped, kept only in memory by Firefox) —
  read from the Firefox session-store snapshot
  (``sessionstore-backups/recovery.jsonlz4``).

Flow reproduced from the network trace:

* ``GET /inputs`` renders the page. The "Scheduled HC" row holds one
  ``<form action="/filtered-data">`` per (date, shift), each with a submit
  ``<button name="record_id" value="{...}">`` whose value is a Python-dict
  string ``{'value':'[ids]','shift':'NS','date_list':'YYYY-MM-DD','team':...}``.
* ``POST /filtered-data`` (``record_id=<value>``) returns the HTML table
  (id ``dataset``) with ``Employee id`` and ``MyTime Status`` columns.

The ELB host carries an account-specific id, so the base URL is
configurable via ``CSP_STATION_EDITOR_URL``.
"""
from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from app.integrations.firefox_cookies import (
    find_firefox_profile_dir,
    read_cookies_from_profile,
    read_sessionstore_cookies,
)


DEFAULT_BASE_URL = "https://cspstationeditor-396255008.eu-west-1.elb.amazonaws.com"
INPUTS_PATH = "/inputs"
FILTERED_DATA_PATH = "/filtered-data"
STATION_EDITOR_TIMEZONE = "Europe/Madrid"
DEFAULT_TIMEOUT = 45.0
DEFAULT_SHIFTS = ("NS", "AMPT")
REQUIRED_COOKIE = "federate_id_token-0"
SESSION_COOKIE = "session"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

_COLUMN_KEYS = {
    "employee id": "employee_id",
    "fclm employee login": "login",
    "fclm shift": "shift",
    "fclm status": "fclm_status",
    "mytime status": "mytime_status",
    "rota status": "rota_status",
    "status for hc calculation": "hc_status",
}


class RosterCheckAuthError(RuntimeError):
    """Raised when the browser session cookies are missing (login required)."""


class RosterCheckError(RuntimeError):
    """Raised on any other unexpected Station Editor response."""


# Last ``session`` cookie seen in the Firefox session-store, kept in memory.
#
# Firefox only mirrors session-scoped cookies into ``recovery.jsonlz4`` every
# ~15 s AND only for tabs that are open when the snapshot is written. The
# toolbox auto-closes the Station Editor login tab as soon as the session
# connects, so the cookie vanishes from the next snapshot even though it is
# still perfectly valid inside Firefox — which used to flip the chip back to
# red and force the operator to reconnect (or restart Firefox, whose shutdown
# write made the cookie visible again). Falling back to the last seen value
# keeps the session usable; the live probe still validates it against the
# server and :func:`_forget_session_cookie` drops it on a real rejection.
_session_cookie_cache: dict[str, str] = {}


def _remember_session_cookie(value: str) -> None:
    if value:
        _session_cookie_cache["value"] = value


def _forget_session_cookie() -> None:
    _session_cookie_cache.pop("value", None)


def base_url() -> str:
    return (os.environ.get("CSP_STATION_EDITOR_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")


def _host() -> str:
    return urlsplit(base_url()).netloc.lower()


def inputs_url() -> str:
    return base_url() + INPUTS_PATH


def _verify_ssl() -> bool:
    """The ELB serves an internal cert not trusted by the OS store; default off."""
    return os.environ.get("CSP_STATION_EDITOR_VERIFY_SSL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def read_cookies() -> dict[str, str]:
    """Merge the persisted ELB cookies with the in-memory ``session`` cookie.

    ``AWSALBAuthNonce`` is expired by the time we read it but the server still
    wants it present, so we include expired cookies for this host.
    """
    host = _host()
    cookies: dict[str, str] = {}
    try:
        profile = find_firefox_profile_dir()
        persisted = read_cookies_from_profile(
            (host, "." + host),
            profile_dir=profile,
            target_host=host,
            include_expired=True,
        )
        cookies.update(persisted or {})
        # The Flask ``session`` cookie is session-scoped → only in the store.
        if SESSION_COOKIE not in cookies:
            store = read_sessionstore_cookies(host, profile_dir=profile)
            cookies.update(store or {})
    except Exception:  # noqa: BLE001 - never blow up the status probe
        pass
    if cookies.get(SESSION_COOKIE):
        # Freshest value wins: a re-login rotates the cookie and the store
        # exposes the new one, which replaces whatever we had cached.
        _remember_session_cookie(cookies[SESSION_COOKIE])
    elif _session_cookie_cache.get("value"):
        # The session-store snapshot no longer carries the cookie (tab closed,
        # flush pending...) but we saw it earlier in this backend run — reuse
        # it; the live probe confirms it is still accepted server-side.
        cookies[SESSION_COOKIE] = _session_cookie_cache["value"]
    return cookies


def _probe_session(cookies: dict[str, str]) -> str:
    """Live check: does the Station Editor actually accept these cookies?

    Issues the same ``GET /inputs`` the extraction starts with and applies
    the same acceptance rule (:func:`fetch_roster`): a 200 whose body carries
    the ``/filtered-data`` forms. A redirect/401/403 — or the login page
    served with a 200 — means the session was invalidated server-side even
    though the cookies still look fine on disk.
    """
    session = _build_session(dict(cookies))
    try:
        resp = session.get(inputs_url(), timeout=15.0, allow_redirects=True)
    except requests.RequestException:
        return "error"
    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
        return "auth"
    if resp.status_code != 200:
        return "error"
    return "ok" if "/filtered-data" in (resp.text or "") else "auth"


def login_status() -> dict[str, Any]:
    """Report whether the operator's Firefox session can be reused.

    The cookie presence check alone is not enough: the server can reject a
    session whose cookies still sit in the profile, which used to show
    "Conectado" while the extraction never started. When the cookies look
    usable we confirm against the server (TTL-cached, see
    :mod:`app.integrations.live_session_probe`); an inconclusive probe
    (network blip) keeps the passive verdict.
    """
    from app.integrations.live_session_probe import cached_probe, cookie_fingerprint

    cookies = read_cookies()
    ready = bool(cookies.get(REQUIRED_COOKIE)) and bool(cookies.get(SESSION_COOKIE))
    missing = [c for c in (REQUIRED_COOKIE, SESSION_COOKIE) if not cookies.get(c)]
    session_rejected = False
    if ready:
        outcome = cached_probe(
            "station-editor",
            cookie_fingerprint(cookies.get(REQUIRED_COOKIE), cookies.get(SESSION_COOKIE)),
            lambda: _probe_session(cookies),
        )
        if outcome == "auth":
            ready = False
            session_rejected = True
            # The server rejected these cookies: a cached ``session`` value is
            # dead weight and would keep producing the same fingerprint (and
            # the same cached rejection), masking a fresh re-login.
            _forget_session_cookie()
    return {
        "ready": ready,
        "missing": missing,
        "session_rejected": session_rejected,
        "cookie_count": len(cookies),
        "login_url": inputs_url(),
    }


def _default_target_date() -> str:
    now = datetime.now(ZoneInfo(STATION_EDITOR_TIMEZONE))
    return (now.date() + timedelta(days=1)).isoformat()


def _normalize_date(value: str | datetime | None) -> str:
    if value is None:
        return _default_target_date()
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        return _default_target_date()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Fecha no parseable: {value!r}")


def _build_session(cookies: dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.verify = _verify_ssl()
    if not session.verify:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # noqa: BLE001
            pass
    session.cookies.update(cookies)
    session.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        }
    )
    return session


def _parse_hc_forms(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    forms: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for button in soup.find_all("button", attrs={"name": "record_id"}):
        raw = button.get("value")
        if not raw:
            continue
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            continue
        if not isinstance(parsed, dict):
            continue
        shift = str(parsed.get("shift") or "").strip().upper()
        date_list = str(parsed.get("date_list") or "").strip()
        if not shift or not date_list:
            continue
        key = (shift, date_list)
        if key in seen:
            continue
        seen.add(key)
        forms.append(
            {
                "shift": shift,
                "date": date_list,
                "team": str(parsed.get("team") or "").strip(),
                "record_id": raw,
            }
        )
    return forms


def _parse_dataset_rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="dataset")
    if table is None:
        return []
    thead = table.find("thead")
    if thead is None:
        return []
    header_rows = thead.find_all("tr")
    if not header_rows:
        return []
    labels = header_rows[-1].find_all("th")
    index_to_key: dict[int, str] = {}
    for idx, th in enumerate(labels):
        key = _COLUMN_KEYS.get(th.get_text(strip=True).lower())
        if key:
            index_to_key[idx] = key

    body = table.find("tbody")
    rows_src = body.find_all("tr") if body else table.find_all("tr")
    out: list[dict[str, str]] = []
    for tr in rows_src:
        cells = tr.find_all("td")
        if not cells:
            continue
        record: dict[str, str] = {}
        for idx, key in index_to_key.items():
            if idx < len(cells):
                record[key] = cells[idx].get_text(strip=True)
        if record.get("employee_id"):
            out.append(record)
    return out


def _check_response(resp: requests.Response) -> None:
    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
        raise RosterCheckAuthError(
            f"El Station Editor rechazó la petición (HTTP {resp.status_code}). "
            "Abre la página en Firefox e inicia sesión, luego reintenta."
        )
    if resp.status_code != 200:
        raise RosterCheckError(
            f"El Station Editor devolvió HTTP {resp.status_code}."
        )


def fetch_roster(
    date: str | datetime | None = None,
    *,
    shifts: tuple[str, ...] | list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Return the Scheduled HC roster for ``date`` (default D+1) and shifts."""
    iso_date = _normalize_date(date)
    want_shifts = tuple(
        s.strip().upper() for s in (shifts or DEFAULT_SHIFTS) if str(s).strip()
    ) or DEFAULT_SHIFTS

    cookies = read_cookies()
    if not cookies.get(REQUIRED_COOKIE) or not cookies.get(SESSION_COOKIE):
        raise RosterCheckAuthError(
            "No hay sesión del Station Editor en Firefox. Abre la página, inicia "
            "sesión y vuelve a intentarlo."
        )

    def _record_auth_reject() -> None:
        """Remember a server-side rejection so ``login_status`` sees it.

        Without this, an auth failure mid-extraction could race a still-fresh
        "ok" entry in the live-probe cache and the chip would flip back to
        "Conectado" right after the failure.
        """
        _forget_session_cookie()
        try:
            from app.integrations.live_session_probe import (
                cookie_fingerprint,
                record_outcome,
            )

            record_outcome(
                "station-editor",
                cookie_fingerprint(cookies.get(REQUIRED_COOKIE), cookies.get(SESSION_COOKIE)),
                "auth",
            )
        except Exception:  # noqa: BLE001 - best-effort cache seed
            pass

    # ``GET /inputs`` rotates the ``federate_id_token`` in its own response,
    # which desyncs it from the ``session`` cookie and makes a follow-up POST
    # 500. So we read the page in a throwaway session (only to parse the
    # forms) and issue each ``/filtered-data`` POST in a fresh session seeded
    # with the ORIGINAL, un-rotated cookies.
    get_session = _build_session(dict(cookies))
    try:
        resp = get_session.get(inputs_url(), timeout=timeout, allow_redirects=True)
    except requests.exceptions.SSLError as exc:
        raise RosterCheckError(f"Error SSL contra el Station Editor: {exc}") from exc
    except requests.RequestException as exc:
        raise RosterCheckError(f"Error de red contra el Station Editor: {exc}") from exc
    try:
        _check_response(resp)
    except RosterCheckAuthError:
        _record_auth_reject()
        raise
    if "/filtered-data" not in resp.text:
        _record_auth_reject()
        raise RosterCheckAuthError(
            "La sesión del Station Editor no es válida. Recarga la página en "
            "Firefox (inicia sesión si hace falta) y reintenta."
        )

    forms = _parse_hc_forms(resp.text)
    day_forms = {
        f["shift"]: f for f in forms if f["date"] == iso_date and f["shift"] in want_shifts
    }

    rows: list[dict[str, str]] = []
    team = ""
    missing_shifts: list[str] = []
    for shift in want_shifts:
        form = day_forms.get(shift)
        if form is None:
            missing_shifts.append(shift)
            continue
        team = team or form.get("team", "")
        post_session = _build_session(dict(cookies))  # original, un-rotated cookies
        try:
            data_resp = post_session.post(
                base_url() + FILTERED_DATA_PATH,
                data={"record_id": form["record_id"]},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": base_url(),
                    "Referer": inputs_url(),
                },
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise RosterCheckError(
                f"Error de red al consultar el turno {shift}: {exc}"
            ) from exc
        try:
            _check_response(data_resp)
        except RosterCheckAuthError:
            _record_auth_reject()
            raise
        for record in _parse_dataset_rows(data_resp.text):
            record.setdefault("shift", shift)
            record["scheduled_shift"] = shift
            rows.append(record)

    by_shift: dict[str, int] = {}
    by_mytime: dict[str, int] = {}
    for record in rows:
        by_shift[record["scheduled_shift"]] = by_shift.get(record["scheduled_shift"], 0) + 1
        status = record.get("mytime_status") or "(vacío)"
        by_mytime[status] = by_mytime.get(status, 0) + 1

    return {
        "date": iso_date,
        "shifts": list(want_shifts),
        "team": team,
        "rows": rows,
        "counts": {"total": len(rows), "by_shift": by_shift, "by_mytime": by_mytime},
        "missing_shifts": missing_shifts,
    }


__all__ = [
    "RosterCheckAuthError",
    "RosterCheckError",
    "base_url",
    "inputs_url",
    "login_status",
    "fetch_roster",
]
