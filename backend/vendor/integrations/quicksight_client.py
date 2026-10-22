"""Amazon QuickSight integration — pulls the EU AMZL DPR tables straight from
the dashboard, reusing the Midway/QuickSight session cookies stored in the
user's Firefox profile (same philosophy as the Mercury client).

The dashboard exposes the four DPR inputs on four different tabs (sheets):

* ``metric_data``    — "EU Metric Data"
* ``focus_areas``    — "Focus Areas"
* ``metric_weight``  — "Metric Weights" (the ``*_wt_final`` columns)
* ``thresholds``     — "Thresholds" (the ``*_t0/t1/t2/t3`` columns)  [pending]

Each tab is a table visual whose data is streamed over a WebSocket. We
replicate that: connect with the cookies, send a ``START_VIS`` message that
references the shared prepared data source (``streamSourceId``) and describes
the visual, then read the ``data`` frame and map the QuickSight column ids to
the DPR column names.

Two values are environment-specific (stable per dashboard/session but they can
rotate); override via env when needed:

* ``QUICKSIGHT_MBTC``              — anti-CSRF token.
* ``QUICKSIGHT_STREAM_SOURCE_ID`` — prepared data source id.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import requests

try:  # websocket-client
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None

from app.integrations.firefox_cookies import (
    QUICKSIGHT_HOST_PATTERNS,
    read_cookies_from_profile,
)


QUICKSIGHT_REGION = "us-east-1"
QUICKSIGHT_HOST = f"{QUICKSIGHT_REGION}.quicksight.aws.amazon.com"
QUICKSIGHT_ALIAS = "amzlbiaquicksight"
DASHBOARD_ID = "cda54de3-f882-4673-9d0a-ebbb6a167a10"
QUICKSIGHT_LOGIN_URL = f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}"

DEFAULT_MBTC = "b6e3n16w1F1SM%2Fd6%2FwtLU7mtC5IHnwPAcAtBpBchRgc%3D"
DEFAULT_STREAM_SOURCE_ID = "9c6f365f-d44b-4472-984e-cdd19d730c1b"

_YEAR_ID = "d_eu_amzl-year-d9e98a"
_COUNTRY_ID = "d_eu_amzl-country-9155e4"
_STATION_ID = "d_eu_amzl-station-a5f124"

# Per-tab config. ``group`` is the ordered list of (column_id, dpr_name) that
# mirrors the ``group`` array each row returns. ``metrics`` maps metric column
# ids to DPR column names. ``limit`` is applied to the last group field (the
# one QuickSight paginates on).
SHEETS: dict[str, dict[str, Any]] = {
    "metric_data": {
        "sheet_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_3c7ba060-69a6-46b1-8091-82c0f6779976",
        "visual_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_0ad99c5f-c791-46ac-a657-aeff9e8d9729",
        "limit": 10000,
        "group": [
            (_YEAR_ID, "year"),
            ("d_eu_amzl-week-3a96c9", "week"),
            (_COUNTRY_ID, "country"),
            (_STATION_ID, "station"),
            ("d_eu_amzl-dsp-24d97a", "dsp"),
            ("d_eu_amzl-status-e5175b", "status"),
            ("d_eu_amzl-boc_compl-6f9462", "boc_compliance"),
        ],
        "metrics": {
            "d_eu_amzl-delivered-8515cd": "delivered",
            "d_eu_amzl-dispatche-5a99d1": "dispatched",
            "d_eu_amzl-dcr-801153": "dcr",
            "d_eu_amzl-dnr_count-03711d": "dnr_count",
            "d_eu_amzl-dnr_dpmo-d6ea54": "dnr_dpmo",
            "d_eu_amzl-dsc_dpmo-a8dabf": "dsc_dpmo",
            # The column-id reads "ndnr" (legacy AMZL name for Loss on
            # Road = Not Delivered Not Returned) but the dashboard
            # labels it "LOR DPMO". METRIC_DEFS expects ``lor_dpmo``.
            "d_eu_amzl-ndnr_dpmo-ab07c7": "lor_dpmo",
            "d_eu_amzl-ndnr_coun-96ff4e": "lor_count",
            "d_eu_amzl-pod_compl-d82797": "pod_compliance",
            "d_eu_amzl-contact_c-247bef": "contact_compliance",
            "d_eu_amzl-capacity_-9e1dfa": "capacity_reliability",
            "d_eu_amzl-ssd_capac-12ae16": "sd/ssd_capacity_reliability",
            "d_eu_amzl-work_hour-7c323c": "work_hour_compliance",
            "d_eu_amzl-cas-687ff4": "cas",
            "d_eu_amzl-ce_dpmo-71a4b6": "ce_dpmo",
            "d_eu_amzl-cdf_dpmo-35df3f": "cdf_dpmo",
            "d_eu_amzl-positive_-84e5ee": "positive_dex",
            "d_eu_amzl-dvic_comp-2d7a07": "dvic_compliance",
            "d_eu_amzl-vsa_compl-039e55": "vsa_compliance",
            "d_eu_amzl-speeding_-28326e": "speeding_event_rate",
            "d_eu_amzl-fico-89f6d1": "fico",
            "d_eu_amzl-ementor_a-a9f11f": "ementor_adoption_rate",
            "d_eu_amzl-sign_sign-242228": "sign_signal_violations_rate",
            "d_eu_amzl-following-dc0ec8": "following_distance_rate",
            "d_eu_amzl-distracti-bb1fdf": "distractions_rate",
            "d_eu_amzl-cret_psb-bab613": "cret_psb",
        },
    },
    "focus_areas": {
        "sheet_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_67061927-cbb0-4f08-a82b-6eb9cc0d01a3",
        "visual_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_8e5d9425-e0cd-4abb-9bb7-1f0470abfe89",
        "limit": 500,
        "group": [
            (_YEAR_ID, "year"),
            ("d_eu_amzl-week-3a96c9", "week"),
            (_COUNTRY_ID, "country"),
            ("d_eu_amzl-dsp-24d97a", "dsp"),
            (_STATION_ID, "station"),
            ("d_eu_amzl-status-e5175b", "status"),
            ("d_eu_amzl-focus_are-9f698f", "focus_area_1"),
            ("d_eu_amzl-focus_are-377f9c", "focus_area_2"),
            ("d_eu_amzl-focus_are-f4f5c4", "focus_area_3"),
        ],
        "metrics": {},
    },
    "metric_weight": {
        "sheet_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_bc5b799d-3fbc-4f0f-b053-b476f9e9199f",
        "visual_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_a47f00d9-4089-4dfc-bbb6-838984487811",
        "limit": 10000,
        "group": [
            (_YEAR_ID, "year"),
            ("d_eu_amzl-week-3a96c9", "week"),
            (_COUNTRY_ID, "country"),
            (_STATION_ID, "station"),
            ("d_eu_amzl-dsp-24d97a", "dsp"),
        ],
        "metrics": {
            "d_eu_amzl-dcr_wt_fi-fa11d6": "dcr_wt_final",
            "d_eu_amzl-dnr_wt_fi-d9f975": "dnr_wt_final",
            "d_eu_amzl-dsc_dpmo_-24d669": "dsc_dpmo_wt_final",
            # See note in metric_data: dashboard label is "LOR" but the
            # column-id keeps the legacy AMZL "ndnr" prefix.
            "d_eu_amzl-ndnr_dpmo-0a55ad": "lor_dpmo_wt_final",
            "d_eu_amzl-pod_wt_fi-232cf4": "pod_wt_final",
            "d_eu_amzl-cc_wt_fin-c1702a": "cc_wt_final",
            "d_eu_amzl-capacity_-fbb510": "capacity_reliability_wt_final",
            "d_eu_amzl-ssd_capac-b5451f": "sd/ssd_capacity_reliability_wt_final",
            "d_eu_amzl-whc_wt_fi-f089a2": "whc_wt_final",
            "d_eu_amzl-cas_wt_fi-22e7ae": "cas_wt_final",
            "d_eu_amzl-ce_dpmo_w-420827": "ce_dpmo_wt_final",
            "d_eu_amzl-positive_-510a18": "positive_dex_wt_final",
            "d_eu_amzl-cdf_dpmo_-930347": "cdf_dpmo_wt_final",
            "d_eu_amzl-speeding_-2c4f69": "speeding_event_rate_wt_final",
            "d_eu_amzl-fico_wt_f-2cf717": "fico_wt_final",
            "d_eu_amzl-ementor_a-f0a472": "ementor_adoption_rate_wt_final",
            "d_eu_amzl-vsa_wt_fi-891831": "vsa_wt_final",
            "d_eu_amzl-dvic_wt_f-587401": "dvic_wt_final",
            "d_eu_amzl-sign_sign-343796": "sign_signal_violations_rate_wt_final",
            "d_eu_amzl-following-c624cc": "following_distance_rate_wt_final",
            "d_eu_amzl-distracti-e38e09": "distractions_rate_wt_final",
            "d_eu_amzl-cret_psb_-34d366": "cret_psb_wt_final",
        },
    },
    "thresholds": {
        "sheet_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_5c1f9de5-bd84-410e-ad95-8f6a722e4e6e",
        "visual_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_cc7dd6ac-7987-459c-a32d-e54c706fefa0",
        "limit": 10000,
        "group": [
            (_YEAR_ID, "year"),
            ("d_eu_amzl-week-3a96c9", "week"),
            (_COUNTRY_ID, "country"),
            (_STATION_ID, "station"),
            ("d_eu_amzl-dsp-24d97a", "dsp"),
        ],
        "metrics": {
            "d_eu_amzl-dcr_t0-2f505e": "dcr_t0",
            "d_eu_amzl-dcr_t1-bbaebf": "dcr_t1",
            "d_eu_amzl-dcr_t2-fbc88c": "dcr_t2",
            "d_eu_amzl-dcr_t3-39b00d": "dcr_t3",
            "d_eu_amzl-dsc_dpmo_-f3c771": "dsc_dpmo_t0",
            "d_eu_amzl-dsc_dpmo_-d32e9e": "dsc_dpmo_t1",
            "d_eu_amzl-dsc_dpmo_-460382": "dsc_dpmo_t2",
            "d_eu_amzl-dsc_dpmo_-1e0f91": "dsc_dpmo_t3",
            # LOR thresholds: legacy "ndnr" id, dashboard label "LOR".
            "d_eu_amzl-ndnr_dpmo-dac361": "lor_dpmo_t0",
            "d_eu_amzl-ndnr_dpmo-b1ab1c": "lor_dpmo_t1",
            "d_eu_amzl-ndnr_dpmo-e7abc8": "lor_dpmo_t2",
            "d_eu_amzl-ndnr_dpmo-b5afc3": "lor_dpmo_t3",
            "d_eu_amzl-pod_t0-4400c6": "pod_t0",
            "d_eu_amzl-pod_t1-dab39d": "pod_t1",
            "d_eu_amzl-pod_t2-b86c30": "pod_t2",
            "d_eu_amzl-pod_t3-0752ee": "pod_t3",
            "d_eu_amzl-cc_t0-04f20e": "cc_t0",
            "d_eu_amzl-cc_t1-9e0253": "cc_t1",
            "d_eu_amzl-cc_t2-0ec750": "cc_t2",
            "d_eu_amzl-cc_t3-2f2c30": "cc_t3",
            "d_eu_amzl-capacity_-55ffa7": "capacity_reliability_t0",
            "d_eu_amzl-capacity_-853402": "capacity_reliability_t1",
            "d_eu_amzl-capacity_-3a4c20": "capacity_reliability_t2",
            "d_eu_amzl-capacity_-021e22": "capacity_reliability_t3",
            "d_eu_amzl-ssd_capac-4cf882": "sd/ssd_capacity_reliability_t0",
            "d_eu_amzl-ssd_capac-8977cb": "sd/ssd_capacity_reliability_t1",
            "d_eu_amzl-ssd_capac-ef6056": "sd/ssd_capacity_reliability_t2",
            "d_eu_amzl-ssd_capac-fb848f": "sd/ssd_capacity_reliability_t3",
            "d_eu_amzl-uwh_t0-575e9d": "uwh_t0",
            "d_eu_amzl-uwh_t1-6b11fe": "uwh_t1",
            "d_eu_amzl-uwh_t2-dfb3f4": "uwh_t2",
            "d_eu_amzl-uwh_t3-5e5ff3": "uwh_t3",
            "d_eu_amzl-ce_dpmo_t-0bce90": "ce_dpmo_t0",
            "d_eu_amzl-ce_dpmo_t-c0c312": "ce_dpmo_t1",
            "d_eu_amzl-ce_dpmo_t-3e6a1d": "ce_dpmo_t2",
            "d_eu_amzl-ce_dpmo_t-39eaf4": "ce_dpmo_t3",
            "d_eu_amzl-dex_t0-e9b6f6": "dex_t0",
            "d_eu_amzl-dex_t1-0bb6a7": "dex_t1",
            "d_eu_amzl-dex_t2-921287": "dex_t2",
            "d_eu_amzl-dex_t3-3e0e1d": "dex_t3",
            "d_eu_amzl-cdf_dpmo_-fabef5": "cdf_dpmo_t0",
            "d_eu_amzl-cdf_dpmo_-f1c38e": "cdf_dpmo_t1",
            "d_eu_amzl-cdf_dpmo_-d4608e": "cdf_dpmo_t2",
            "d_eu_amzl-cdf_dpmo_-e91fcd": "cdf_dpmo_t3",
            "d_eu_amzl-speeding_-c1cf0b": "speeding_event_t0",
            "d_eu_amzl-speeding_-3afe35": "speeding_event_t1",
            "d_eu_amzl-speeding_-9fbc97": "speeding_event_t2",
            "d_eu_amzl-speeding_-80c392": "speeding_event_t3",
            "d_eu_amzl-fico_t0-1072a5": "fico_t0",
            "d_eu_amzl-fico_t1-7acf89": "fico_t1",
            "d_eu_amzl-fico_t2-28c8ff": "fico_t2",
            "d_eu_amzl-fico_t3-937aab": "fico_t3",
            "d_eu_amzl-ementor_a-e30cc7": "ementor_adoption_t0",
            "d_eu_amzl-ementor_a-13532e": "ementor_adoption_t1",
            "d_eu_amzl-ementor_a-1c529f": "ementor_adoption_t2",
            "d_eu_amzl-ementor_a-d0281a": "ementor_adoption_t3",
            "d_eu_amzl-vsa_t0-08527b": "vsa_t0",
            "d_eu_amzl-vsa_t1-368e1b": "vsa_t1",
            "d_eu_amzl-vsa_t2-eeddfe": "vsa_t2",
            "d_eu_amzl-vsa_t3-6d7d92": "vsa_t3",
            "d_eu_amzl-dvic_t0-d36db2": "dvic_t0",
            "d_eu_amzl-dvic_t1-f3ad7b": "dvic_t1",
            "d_eu_amzl-dvic_t2-cac0a6": "dvic_t2",
            "d_eu_amzl-dvic_t3-218a8a": "dvic_t3",
            "d_eu_amzl-sign_sign-56013e": "sign_signal_violations_rate_t0",
            "d_eu_amzl-sign_sign-18478f": "sign_signal_violations_rate_t1",
            "d_eu_amzl-sign_sign-abfb2b": "sign_signal_violations_rate_t2",
            "d_eu_amzl-sign_sign-02e1e1": "sign_signal_violations_rate_t3",
            "d_eu_amzl-following-008d58": "following_distance_rate_t0",
            "d_eu_amzl-following-d950fb": "following_distance_rate_t1",
            "d_eu_amzl-following-c1ee92": "following_distance_rate_t2",
            "d_eu_amzl-following-84e4c9": "following_distance_rate_t3",
            "d_eu_amzl-distracti-14757d": "distractions_rate_t0",
            "d_eu_amzl-distracti-3acb24": "distractions_rate_t1",
            "d_eu_amzl-distracti-14bf1b": "distractions_rate_t2",
            "d_eu_amzl-distracti-6d73f9": "distractions_rate_t3",
            "d_eu_amzl-cret_psb_-f18a40": "cret_psb_t0",
            "d_eu_amzl-cret_psb_-3c89fe": "cret_psb_t1",
            "d_eu_amzl-cret_psb_-0850f1": "cret_psb_t2",
            "d_eu_amzl-cret_psb_-3d6c1b": "cret_psb_t3",
        },
    },
    # "EU Network Score" tab: the official scorecard total per DSP/week.
    # The DPR shows this value; the locally derived score is only a
    # fallback when this tab is unavailable.
    "network_score": {
        "sheet_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_06b72c8c-db06-4805-89b0-22c7b0d3ad67",
        "visual_id": "cda54de3-f882-4673-9d0a-ebbb6a167a10_4f7586fb-c6ca-4c62-8214-a02b86b6a933",
        "limit": 10000,
        "group": [
            (_YEAR_ID, "year"),
            ("d_eu_amzl-week-3a96c9", "week"),
            (_STATION_ID, "station"),
            ("d_eu_amzl-dsp-24d97a", "dsp"),
        ],
        "metrics": {
            "d_eu_amzl-total_sco-bdde3c": "total_score",
        },
    },
}

class QuickSightAuthError(RuntimeError):
    """No usable QuickSight session cookies in the Firefox profile."""


class QuickSightError(RuntimeError):
    """QuickSight returned an error frame or no data."""


def _mbtc() -> str:
    return os.environ.get("QUICKSIGHT_MBTC", "").strip() or DEFAULT_MBTC


def _fetch_mbtc(cookies: dict[str, str], timeout: float = 15.0) -> str:
    """Extract the current CSRF token (mbtc) from the dashboard HTML page.

    QuickSight embeds a per-session ``csrf`` token in the dashboard page.
    The hardcoded DEFAULT_MBTC rotates and gets rejected with
    "csrf token missing or not match", so we must read the live one.

    Returns the csrf token (already URL-encoded as it appears in the page),
    or falls back to the configured/default mbtc if extraction fails.
    """
    import re
    import html as _html

    url = f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}"
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
                "Accept": "text/html,application/xhtml+xml",
            },
            cookies=cookies, timeout=timeout, allow_redirects=False,
        )
        if resp.status_code != 200:
            return _mbtc()
        body = resp.text
        # The page contains: &quot;csrf&quot;:&quot;<token>&quot;
        # (HTML-escaped JSON). Decode entities then match the csrf value.
        decoded = _html.unescape(body)
        m = re.search(r'"csrf"\s*:\s*"([^"]+)"', decoded)
        if m:
            return m.group(1)
    except requests.RequestException:
        pass
    return _mbtc()


def _stream_source_id() -> str:
    return os.environ.get("QUICKSIGHT_STREAM_SOURCE_ID", "").strip() or DEFAULT_STREAM_SOURCE_ID


def _read_cookies() -> dict[str, str]:
    try:
        raw = read_cookies_from_profile(
            QUICKSIGHT_HOST_PATTERNS,
            target_host=QUICKSIGHT_HOST,
            include_expired=True,
        )
        # Strip wrapping double-quotes from cookie values. Firefox stores them
        # literally as received from Set-Cookie (e.g. aws-ds-access-token="..."),
        # but they must be sent back without the quotes.
        return {k: v.strip('"') for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _active_alias(cookies: dict[str, str]) -> str:
    """Return the QuickSight account alias to use for API/WebSocket calls.

    Always use QUICKSIGHT_ALIAS (amzlbiaquicksight) — that's the account
    where the DPR dashboard lives.
    """
    return QUICKSIGHT_ALIAS


def _is_session_for_alias(cookies: dict[str, str]) -> bool:
    """Check whether the active QuickSight session belongs to our alias.

    If the most recent session in qs-saved-accounts is NOT amzlbiaquicksight,
    QuickSight will reject/redirect our requests. The user must open the
    dashboard URL to switch the active session.
    """
    import urllib.parse

    raw = cookies.get("qs-saved-accounts", "")
    if not raw:
        return False
    try:
        decoded = urllib.parse.unquote(raw)
        data = json.loads(decoded)
        accounts = data.get("a", [])
        if not accounts:
            return False
        # The account with the highest timestamp is the active one.
        accounts.sort(key=lambda x: x.get("t", 0), reverse=True)
        most_recent = accounts[0].get("an", "")
        return most_recent == QUICKSIGHT_ALIAS
    except Exception:  # noqa: BLE001
        return False


def quicksight_login_status() -> dict[str, Any]:
    """Check if the QuickSight session actually works (real server call).

    A session is "ready" ONLY if the server accepts our cookies and the
    WebSocket transport required for data extraction is installed. We read
    cookies via DB+WAL (Firefox's freshest on-disk state) and hit the
    dashboard API. If the server returns data, ready=True. If it returns
    an auth error or expired token, ready=False → the user must log in.
    """
    if websocket is None:
        return {
            "ready": False,
            "cookie_count": 0,
            "correct_account": False,
            "host": QUICKSIGHT_HOST,
            "reason": "missing_dependency",
        }

    cookies = _read_cookies()
    if not cookies:
        return {
            "ready": False,
            "cookie_count": 0,
            "correct_account": False,
            "host": QUICKSIGHT_HOST,
            "reason": "no_cookies",
        }

    correct_alias = _is_session_for_alias(cookies)
    mbtc = _fetch_mbtc(cookies)
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEETS['metric_data']['sheet_id']}?Operation=GetSheetForDashboard&pageSize=1"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept": "*/*",
        "x-amzn-csrf-token": mbtc,
        "x-amzn-target": "api",
        "Referer": f"https://{QUICKSIGHT_HOST}/sn/account/{QUICKSIGHT_ALIAS}/dashboards/{DASHBOARD_ID}",
    }
    try:
        resp = requests.get(
            url, headers=headers, cookies=cookies,
            timeout=12, allow_redirects=False,
        )
        body = resp.text.lower()
        live = (
            resp.status_code == 200
            and "authenticate" not in body
            and "error" not in body[:50]
        )
        return {
            "ready": live,
            "cookie_count": len(cookies),
            "correct_account": correct_alias,
            "host": QUICKSIGHT_HOST,
            "reason": "" if live else ("wrong_account" if not correct_alias else "session_expired"),
        }
    except requests.RequestException:
        return {
            "ready": False,
            "cookie_count": len(cookies),
            "correct_account": correct_alias,
            "host": QUICKSIGHT_HOST,
            "reason": "network_error",
        }


def _metric_value(cell: Any) -> float | None:
    if not isinstance(cell, dict) or not cell:
        return None
    val = cell.get("sum")
    if val is None:
        val = cell.get("custom")
    if isinstance(val, dict):
        val = val.get("parsedValue", val.get("source"))
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _filter_list(values: list[str]) -> str:
    return ",".join(f'"{v}"' for v in values)


def _build_start_vis(
    cfg: dict[str, Any], year: int, country: str, stations: list[str]
) -> dict[str, Any]:
    group = cfg["group"]
    last = len(group) - 1
    fields = []
    for i, (cid, _name) in enumerate(group):
        if i == last:
            fields.append({
                "name": cid, "limit": cfg.get("limit", 10000),
                "sort": {"name": _YEAR_ID, "dir": "DESC"},
                "chartContext": cid, "offset": 0, "partitioned": False,
            })
        else:
            fields.append({
                "name": cid, "limit": -1, "sort": None,
                "chartContext": cid, "offset": -1, "partitioned": False,
            })
    metrics = [{"name": cid, "func": "SUM", "chartContext": cid} for cid in cfg["metrics"]]
    # Always add the count calc QuickSight ships (required for metric-less tabs).
    metrics.append({"name": "!calc.count.0.0", "func": "CUSTOM"})
    filters = [
        f"((in({{{_YEAR_ID}}},[{int(year)}],true)))",
        f'((in({{{_COUNTRY_ID}}},[{_filter_list([country])}],true)))',
        f'((in({{{_STATION_ID}}},[{_filter_list(stations)}],true)))',
    ]
    return {
        "type": "START_VIS",
        "cid": str(uuid.uuid4()),
        "request": {
            "dashboardId": DASHBOARD_ID,
            "cfg": {
                "excludeCountStar": True,
                "group": {"fields": fields, "metrics": metrics},
                "clsSupport": True,
                "calculatedFields": [{"alias": "!calc.count.0.0", "func": "countOver(1)"}],
                "filterExpressions": filters,
                "parameters": {},
            },
            "columnNamingMode": "ID",
            "speed": 1,
            "tz": "Europe/Berlin",
            "streamSourceId": _stream_source_id(),
            "datasetVersionAlias": "$STABLE",
            "enableComputations": False,
            "computationsOnly": False,
            "sheetId": cfg["sheet_id"],
            "visualId": cfg["visual_id"],
            "visualArn": None,
            "visualType": "TABLE",
            "fetchAsyncQueryResult": False,
            "chartId": str(uuid.uuid4()),
            "callingContextId": str(uuid.uuid4()),
        },
        "queued": True,
    }


def _extract_set_cookies(resp: "requests.Response") -> dict[str, str]:
    """Parse Set-Cookie headers and return {name: value} for non-empty values.

    We parse raw headers because ``resp.cookies`` filters by Path and
    misses cookies like ``aws-ds-access-token`` (path=/sn/account/...).
    """
    from http.cookies import SimpleCookie

    extracted: dict[str, str] = {}
    # Access raw headers from the underlying urllib3 response.
    raw_headers: list[str] = []
    try:
        if resp.raw and hasattr(resp.raw, "headers"):
            raw_headers = resp.raw.headers.getlist("Set-Cookie") or []
    except Exception:  # noqa: BLE001
        pass
    if not raw_headers:
        # Fallback: single header value (may be comma-joined, rare for Set-Cookie)
        single = resp.headers.get("Set-Cookie", "")
        if single:
            raw_headers = [single]
    for raw in raw_headers:
        if not raw:
            continue
        # Quick manual parse: first key=value before the first ";"
        parts = raw.split(";", 1)
        if "=" not in parts[0]:
            continue
        name, _, value = parts[0].partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        # Skip cookies being cleared (empty value or expired-in-1970 trick)
        if name and value:
            extracted[name] = value
    return extracted


def _warm_up(cookies: dict[str, str], mbtc: str, timeout: float) -> dict[str, str]:
    """Hit the dashboard API to trigger QuickSight's session bootstrap.

    QuickSight often responds with a 403 on the first call BUT sets new
    session cookies (most importantly ``aws-ds-access-token``) in the
    ``Set-Cookie`` headers. A second call with those cookies included
    completes the session bootstrap (returns 200). We capture and merge
    all response cookies so the subsequent WebSocket handshake succeeds.

    Returns the updated cookie dict (original + any new ones from the responses).
    """
    alias = _active_alias(cookies)
    url = (
        f"https://{QUICKSIGHT_HOST}/sn/account/{alias}/api/dashboards/{DASHBOARD_ID}"
        f"/sheets/{SHEETS['metric_data']['sheet_id']}?Operation=GetSheetForDashboard&pageSize=175"
        f"&customViewSubentityOptions=IGNORE_CUSTOM_VIEW&mbtc={mbtc}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept": "*/*", "X-Amzn-Web-Client-Version": "0.1.4",
        "x-amzn-csrf-token": mbtc, "x-amzn-target": "api",
        "Referer": f"https://{QUICKSIGHT_HOST}/sn/account/{alias}/dashboards/{DASHBOARD_ID}",
    }
    merged = dict(cookies)

    # --- Pass 1: trigger session bootstrap (often 403 with Set-Cookie) ---
    try:
        resp = requests.get(
            url, headers=headers, cookies=merged,
            timeout=timeout, allow_redirects=False,
        )
        new_cookies = _extract_set_cookies(resp)
        merged.update(new_cookies)

        # --- Pass 2: retry with the new cookies so the session is fully active ---
        if resp.status_code in (403, 401):
            resp2 = requests.get(
                url, headers=headers, cookies=merged,
                timeout=timeout, allow_redirects=False,
            )
            new_cookies2 = _extract_set_cookies(resp2)
            merged.update(new_cookies2)
    except requests.RequestException:
        pass
    return merged


def _parse_handshake_cookies(exc: Exception) -> dict[str, str]:
    """Try to extract Set-Cookie values from a WebSocket handshake failure.

    The websocket-client library encodes the response headers in the
    exception message as a stringified dict. We try to extract cookies
    from that.
    """
    extracted: dict[str, str] = {}
    msg = str(exc)
    # The format is: "Handshake status 403 ... -+-+- {headers_dict} -+-+- body"
    # Try to find the dict between the -+-+- delimiters.
    parts = msg.split("-+-+-")
    if len(parts) < 2:
        return extracted
    headers_str = parts[1].strip()
    try:
        headers_dict = json.loads(headers_str.replace("'", '"'))
    except Exception:  # noqa: BLE001
        # Fallback: parse with ast.literal_eval
        try:
            import ast
            headers_dict = ast.literal_eval(headers_str)
        except Exception:  # noqa: BLE001
            return extracted
    # Look for set-cookie in the headers (case-insensitive key)
    raw_cookie = None
    for key, value in headers_dict.items():
        if key.lower() == "set-cookie":
            raw_cookie = value
            break
    if not raw_cookie:
        return extracted
    # set-cookie may contain multiple cookies separated by commas (unusual) or
    # be a single long string with "; " separators between attributes.
    # Split on known cookie-name boundaries.
    for segment in raw_cookie.split("; "):
        if "=" in segment and not any(
            segment.strip().lower().startswith(attr)
            for attr in ("path=", "domain=", "expires=", "max-age=", "secure", "httponly", "version=", "samesite=")
        ):
            name, _, value = segment.partition("=")
            name = name.strip()
            value = value.strip().strip('"')
            if name and value:
                extracted[name] = value
    return extracted


def fetch_sheet_rows(
    sheet_key: str,
    *,
    year: int,
    country: str = "ES",
    stations: list[str] | None = None,
    timeout: float = 25.0,
    cookies: dict[str, str] | None = None,
    mbtc: str | None = None,
) -> list[dict[str, Any]]:
    """Pull one DPR tab from QuickSight. Returns DPR-keyed row dicts."""
    if websocket is None:
        raise QuickSightError("Falta la dependencia 'websocket-client'.")
    cfg = SHEETS.get(sheet_key)
    if cfg is None:
        raise QuickSightError(f"Tab QuickSight desconocida: {sheet_key}")
    stations = [s.strip().upper() for s in (stations or ["DCZ4"]) if s and s.strip()]
    if not stations:
        stations = ["DCZ4"]

    cookies = cookies if cookies is not None else _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{QUICKSIGHT_HOST}/ en Firefox."
        )
    if mbtc is None:
        mbtc = _fetch_mbtc(cookies, timeout=timeout)
    alias = _active_alias(cookies)
    ws_url = f"wss://{QUICKSIGHT_HOST}/sn/account/{alias}/websocket/?mbtc={mbtc}"

    # Attempt WebSocket connection with retry: if the first handshake returns
    # 403 with new session cookies (Set-Cookie in the error), capture them and
    # retry once.
    ws = None
    for attempt in range(2):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            ws = websocket.create_connection(
                ws_url,
                header=[
                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
                    "Origin: https://" + QUICKSIGHT_HOST,
                ],
                cookie=cookie_header, timeout=timeout, suppress_origin=True,
            )
            break  # Connected successfully
        except Exception as exc:  # noqa: BLE001
            exc_str = str(exc)
            # "Handshake status 200" with auth error body means session expired.
            if "(re)authenticate" in exc_str or "return to https://console" in exc_str:
                raise QuickSightAuthError(
                    "Sesion de AWS expirada. Cierra la pestana de QuickSight en Firefox, "
                    "vuelve a abrirla para renovar la sesion, y reintenta."
                )
            if attempt == 0 and "403" in exc_str:
                # Try to extract cookies from the handshake failure
                new_cookies = _parse_handshake_cookies(exc)
                if new_cookies:
                    cookies = {**cookies, **new_cookies}
                    continue
            if attempt == 0 and ("302" in exc_str or "url is invalid" in exc_str.lower()):
                # 302 redirect = wrong alias active, or session redirect to login.
                raise QuickSightAuthError(
                    "Sesion de QuickSight no activa en la cuenta correcta. "
                    "Abre el dashboard en Firefox e inicia sesion."
                )
            raise QuickSightAuthError(
                f"No se pudo conectar al WebSocket de QuickSight: {exc_str[:200]}"
            )

    if ws is None:
        raise QuickSightAuthError("No se pudo conectar al WebSocket de QuickSight.")

    start_vis = _build_start_vis(cfg, year, country, stations)
    cid = start_vis["cid"]
    data_msg = None
    try:
        ws.send(json.dumps(start_vis))
        ws.settimeout(timeout)
        deadline = time.time() + timeout + 5
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if "error" in msg:
                raise QuickSightError(f"QuickSight error: {msg.get('error')} - {msg.get('details')}")
            if "data" in msg and "available" in msg:
                data_msg = msg
                break
    finally:
        try:
            ws.send(json.dumps({"type": "STOP_VIS", "cids": [cid]}))
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass

    if not data_msg:
        raise QuickSightError(f"QuickSight no devolvio datos para '{sheet_key}'.")

    group = cfg["group"]
    metrics_map = cfg["metrics"]
    rows: list[dict[str, Any]] = []
    for item in data_msg.get("data", []):
        g = item.get("group", [])
        mcells = (item.get("current") or {}).get("metrics", {})
        record: dict[str, Any] = {}
        for idx, (_cid, name) in enumerate(group):
            record[name] = g[idx] if idx < len(g) else None
        for col_id, dpr_name in metrics_map.items():
            record[dpr_name] = _metric_value(mcells.get(col_id))
        rows.append(record)
    return rows


def fetch_all_tables(
    *, year: int, country: str = "ES", stations: list[str] | None = None, timeout: float = 25.0
) -> dict[str, list[dict[str, Any]]]:
    """Fetch every configured DPR tab. Returns ``{kind: rows}``.

    Reuses one cookie read across tabs. Tabs that fail individually are
    skipped (the caller can still build from the rest)."""
    cookies = _read_cookies()
    if not cookies:
        raise QuickSightAuthError(
            f"No hay cookies de QuickSight. Inicia sesion en https://{QUICKSIGHT_HOST}/ en Firefox."
        )
    # Fetch the live CSRF token (mbtc) from the dashboard page — the
    # hardcoded default rotates and gets rejected with "csrf token missing".
    mbtc = _fetch_mbtc(cookies, timeout=timeout)
    # Warm-up captures Set-Cookie tokens (aws-ds-access-token) needed by the WS.
    cookies = _warm_up(cookies, mbtc, timeout)
    out: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for key in SHEETS:
        try:
            out[key] = fetch_sheet_rows(
                key, year=year, country=country, stations=stations,
                timeout=timeout, cookies=cookies, mbtc=mbtc,
            )
        except Exception as exc:  # noqa: BLE001
            errors[key] = str(exc)
    if "metric_data" not in out:
        raise QuickSightError(
            "No se pudo traer Metric Data de QuickSight: " + errors.get("metric_data", "?")
        )
    out["_errors"] = errors  # type: ignore[assignment]
    return out


__all__ = [
    "DASHBOARD_ID",
    "QUICKSIGHT_HOST",
    "QUICKSIGHT_LOGIN_URL",
    "SHEETS",
    "QuickSightAuthError",
    "QuickSightError",
    "fetch_all_tables",
    "fetch_sheet_rows",
    "quicksight_login_status",
]
