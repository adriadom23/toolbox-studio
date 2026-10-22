"""Volumen de "Camiones a revisar" para Flow Meeting NS (FCs y SCs).

Dos fuentes, ambas con la sesión Midway del operador en Firefox:

* **FC (rodeo-dub)** — ``https://rodeo-dub.amazon.com/<FC>/Package`` con el
  informe agrupado WORK_POOL x SORT_CODE. La página contiene una tabla por
  sort code (``AMZL-<ST>-ND``); leemos la fila ``Total`` de la tabla de la
  estación configurada y devolvemos la cantidad de paquetes de la columna
  "Range Total" (equivalente al xpath ``.../tr[4]/td[3]/span[1]/a`` grabado
  por el operador).

* **SC (vista de sortcenter)** — ``https://trans-logistics-eu.amazon.com/
  sortcenter/vista/``. Reproducimos las llamadas del HAR del operador:
  seleccionar el nodo (SC), listar los inbound loads
  (``getInboundLoadsIds``) y sumar los "total packages"
  (``getInboundCounts``) de los camiones cuya ruta referencia la estación.

Errores de sesión levantan :class:`TrucksReviewAuthError` con la URL de
login para que el frontend abra la pestaña de identificación (mismo patrón
que SCC / ADTA).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

from app.integrations.firefox_cookies import read_cookies_from_profile

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)

# La URL exacta que el operador usa (grabada en el HAR), con el FC como
# placeholder. Los filtros (work pools, box types...) se mantienen tal cual.
RODEO_URL_TEMPLATE = (
    "https://rodeo-dub.amazon.com/{site}/Package?yAxis=WORK_POOL&zAxis=SORT_CODE"
    "&ShipmentTypes=PACKAGES&exSDRange.quickRange=TODAY&exSDRange.dailyStart=00%3A00"
    "&exSDRange.dailyEnd=00%3A00&boxTypes=STANDARD_BOX&boxTypes=ENVELOPE"
    "&boxTypes=VARIABLE_DEPTH_BOX&boxTypes=FOLDER&boxTypes=BOX_ON_DEMAND&boxTypes=BAG"
    "&boxTypes=OVERWRAP&boxTypes=SHIP_IN_OWN_CONTAINER&boxTypes=RAVIOLI"
    "&boxTypes=CUSTOM_PACKAGING&boxTypes=UNKNOWN&boxTypes=INJECTED_PACKAGE"
    "&_boxTypes=on&workPool=Picking&workPool=PendingInjection&workPool=WorkInProgress"
    "&workPool=Injected&workPool=Shipping&_workPool=on&processTypes=NORMAL"
    "&processTypes=SIOC&processTypes=TEAM_MECH_LIFT&processTypes=UNKNOWN"
    "&_processTypes=on&shipMethod=&sortCode="
)

VISTA_URL = "https://trans-logistics-eu.amazon.com/sortcenter/vista/"
_VISTA_CONTROLLER = "https://trans-logistics-eu.amazon.com/sortcenter/vista/controller/"


class TrucksReviewError(RuntimeError):
    """Fallo de red / parseo al leer el volumen de un site."""


class TrucksReviewAuthError(TrucksReviewError):
    """Sesión Midway caducada; ``login_url`` indica dónde re-autenticarse."""

    def __init__(self, message: str, login_url: str) -> None:
        super().__init__(message)
        self.login_url = login_url


def _looks_like_login(response: requests.Response) -> bool:
    """True cuando la respuesta es la pantalla de SSO (Midway/sentry)."""
    if response.status_code in (401, 403):
        return True
    final = str(response.url or "")
    if "midway-auth" in final or "sentry.amazon.com" in final or "idp" in final.lower():
        return True
    text = response.text[:4000] if response.text else ""
    return "midway" in text.lower() and "authenticate" in text.lower()


# ---------------------------------------------------------------------------
# FC — rodeo-dub
# ---------------------------------------------------------------------------


def _rodeo_cookies() -> dict[str, str]:
    return read_cookies_from_profile(
        ("rodeo-dub.amazon.com", ".amazon.com"),
        target_host="rodeo-dub.amazon.com",
    )


def _int_from_text(text: str) -> int | None:
    cleaned = re.sub(r"[^\d]", "", str(text or ""))
    return int(cleaned) if cleaned else None


def _rodeo_table_range_total(table_html: str) -> int | None:
    """Cantidad de paquetes de la fila ``Total`` / columna "Range Total".

    Equivale al xpath grabado (``tr[4]/td[3]/span[1]/a``) pero buscando por
    etiquetas en lugar de posiciones: la fila cuyo ``<th>`` es ``Total``
    (fallback: la última) y la columna cuyo header contiene ``Range``
    (fallback: la 3ª). Dentro de la celda, el primer ``<a>`` es la cantidad
    (``span.package-quantity``).
    """
    rows = re.findall(r"<tr.*?</tr>", table_html, re.S)
    if not rows:
        return None

    # Índice de la columna "Range Total" entre las columnas de valores.
    col_idx = 2  # Total / Earlier Total / Range Total → 3ª celda (0-based 2)
    header_ths = re.findall(r"<th[^>]*>(.*?)</th>", rows[0], re.S)
    value_headers = [re.sub(r"<[^>]+>", " ", h) for h in header_ths[1:]]
    for i, header in enumerate(value_headers):
        if "range" in header.lower():
            col_idx = i
            break

    total_row = None
    for row in rows[1:]:
        th = re.search(r"<th[^>]*>(.*?)</th>", row, re.S)
        label = re.sub(r"<[^>]+>", " ", th.group(1)).strip().lower() if th else ""
        if label == "total":
            total_row = row
            break
    if total_row is None:
        total_row = rows[-1]

    tds = re.findall(r"<td.*?</td>", total_row, re.S)
    if col_idx >= len(tds):
        return None
    cell = tds[col_idx]
    anchor = re.search(r"<a [^>]*>([\d.,\s]+)</a>", cell)
    if anchor:
        return _int_from_text(anchor.group(1))
    empty = re.search(r'empty-agg"\s*>\s*([\d.,]+)', cell)
    if empty:
        return _int_from_text(empty.group(1))
    return None


def fetch_fc_volume(fc_site: str, station: str, *, timeout: float = 60.0) -> int:
    """Paquetes en ``fc_site`` (rodeo) con sort code de ``station`` (Range Total)."""
    site = str(fc_site or "").strip().upper()
    station = str(station or "").strip().upper()
    if not site or not station:
        raise TrucksReviewError("site/station vacíos")

    url = RODEO_URL_TEMPLATE.format(site=site)
    try:
        cookies = _rodeo_cookies()
    except Exception as exc:  # noqa: BLE001 - perfil de Firefox ausente
        raise TrucksReviewAuthError(str(exc), url) from exc
    if not cookies.get("amzn_sso_token"):
        raise TrucksReviewAuthError("Sin sesión de rodeo (amzn_sso_token)", url)

    try:
        response = requests.get(
            url,
            cookies=cookies,
            headers={"User-Agent": _UA},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise TrucksReviewError(f"rodeo no responde: {exc}") from exc

    if _looks_like_login(response):
        raise TrucksReviewAuthError("Sesión de rodeo caducada", url)
    if response.status_code != 200:
        raise TrucksReviewError(f"rodeo devolvió HTTP {response.status_code}")

    html = response.text or ""
    # Una tabla por sort code; la(s) de la estación llevan el código en el
    # id (p. ej. ``AMZL-DCZ4-NDTable``). Si hay variantes (ND / ND-MR) se
    # suman.
    total = 0
    found = False
    for match in re.finditer(
        r'<table id="([^"]*' + re.escape(station) + r'[^"]*)".*?</table>', html, re.S
    ):
        value = _rodeo_table_range_total(match.group(0))
        if value is not None:
            total += value
            found = True
    if not found:
        raise TrucksReviewError(
            f"rodeo: no se encontró la tabla de {station} en {site}"
        )
    return total


# ---------------------------------------------------------------------------
# SC — vista de sortcenter
# ---------------------------------------------------------------------------


def _vista_cookies() -> dict[str, str]:
    return read_cookies_from_profile(
        (
            "trans-logistics-eu.amazon.com",
            ".trans-logistics-eu.amazon.com",
            ".amazon.com",
        ),
        target_host="trans-logistics-eu.amazon.com",
    )


def _vista_call(
    session: requests.Session,
    csrf: str,
    endpoint: str,
    json_obj: dict[str, Any],
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """POST al controller del vista (form ``anti-csrftoken-a2z`` + ``jsonObj``)."""
    try:
        response = session.post(
            _VISTA_CONTROLLER + endpoint,
            data={
                "anti-csrftoken-a2z": csrf,
                "jsonObj": json.dumps(json_obj, separators=(",", ":")),
            },
            headers={
                "User-Agent": _UA,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": VISTA_URL,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise TrucksReviewError(f"vista no responde ({endpoint}): {exc}") from exc
    if _looks_like_login(response):
        raise TrucksReviewAuthError("Sesión del vista caducada", VISTA_URL)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TrucksReviewError(f"vista {endpoint}: respuesta no JSON") from exc
    if not payload.get("ok", True):
        raise TrucksReviewError(
            f"vista {endpoint}: {str(payload.get('message', ''))[:120]}"
        )
    return payload.get("ret") or {}


def _sum_counts(load_detail: dict[str, Any]) -> int:
    """Suma los COUNT de todas las listas de conteo de un load (total packages)."""
    total = 0
    for key in (
        "preFacilityCounts",
        "processedCounts",
        "unmanifestedLoadedCount",
        "unmanifestedSlamCount",
        "unmanifestedPreSlamCount",
    ):
        for bucket in load_detail.get(key) or []:
            flow = (bucket or {}).get("flowUnitsMap") or {}
            try:
                total += int(round(float(flow.get("COUNT", 0) or 0)))
            except (TypeError, ValueError):
                continue
    return total


def fetch_sc_volume(sc_site: str, station: str, *, timeout: float = 60.0) -> int:
    """Total packages de los camiones del ``sc_site`` cuya ruta referencia ``station``.

    Reproduce el flujo del vista grabado por el operador: seleccionar el SC,
    listar los inbound loads del sort plan y sumar el "Total Packages"
    (``getInboundCounts``, HIGH_LEVEL_LOGICAL_COUNT) de los loads que
    coinciden con la estación.
    """
    site = str(sc_site or "").strip().upper()
    station = str(station or "").strip().upper()
    if not site or not station:
        raise TrucksReviewError("site/station vacíos")

    try:
        cookies = _vista_cookies()
    except Exception as exc:  # noqa: BLE001
        raise TrucksReviewAuthError(str(exc), VISTA_URL) from exc

    session = requests.Session()
    session.cookies.update(cookies)

    # 1) Página del vista → anti-CSRF token (meta ``csrf-token``).
    try:
        page = session.get(
            VISTA_URL, headers={"User-Agent": _UA}, timeout=timeout, allow_redirects=True
        )
    except requests.RequestException as exc:
        raise TrucksReviewError(f"vista no responde: {exc}") from exc
    if _looks_like_login(page):
        raise TrucksReviewAuthError("Sesión del vista caducada", VISTA_URL)
    token_match = re.search(
        r'<meta content="([^"]+)" name="csrf-token"', page.text or ""
    ) or re.search(r'name="csrf-token"\s+content="([^"]+)"', page.text or "")
    if not token_match:
        raise TrucksReviewAuthError("Vista sin csrf-token (¿sesión caducada?)", VISTA_URL)
    csrf = token_match.group(1)

    now_ms = int(time.time() * 1000)

    # 2) Configuración del nodo → ventana del sort plan.
    node_cfg = _vista_call(
        session, csrf, "getNodeConfiguration",
        {"nodeId": site, "searchTime": now_ms, "entity": "getNodeConfiguration"},
        timeout=timeout,
    )
    window = (
        (node_cfg.get("getNodeConfigurationOutput") or {}).get("sortPlanWindow") or {}
    )
    try:
        start = int(window.get("windowStartTime") or now_ms - 12 * 3600_000)
        end = int(window.get("windowEndTime") or now_ms + 24 * 3600_000)
    except (TypeError, ValueError):
        start, end = now_ms - 12 * 3600_000, now_ms + 24 * 3600_000

    # 3) CPTs del sort plan.
    props = _vista_call(
        session, csrf, "getSortPlanProperties",
        {
            "nodeId": site,
            "startTime": start,
            "endTime": end,
            "searchTime": now_ms,
            "propertyName": "CPT",
            "entity": "getSortPlanProperties",
        },
        timeout=timeout,
    )
    cpts = [
        str(c)
        for c in ((props.get("getSortPlanPropertiesOutput") or {}).get("propertyDetails") or [])
    ]
    if not cpts:
        raise TrucksReviewError(f"vista: sin CPTs para {site}")

    # 4) Inbound loads por CPT → filtrar los que referencian la estación.
    loads_out = _vista_call(
        session, csrf, "getInboundLoadsIds",
        {
            "nodeId": site,
            "searchTime": now_ms,
            "entity": "getInboundLoadIds",
            "cpts": cpts,
        },
        timeout=timeout,
    )
    time_map = (
        (loads_out.get("getInboundLoadIdsOutput") or {}).get("timeBasedLoadIdsMap") or {}
    )
    matched: dict[str, dict[str, Any]] = {}
    for load_list in time_map.values():
        for load in load_list or []:
            route = str((load or {}).get("inboundRoute") or "").upper()
            load_id = str((load or {}).get("loadId") or "")
            if load_id and station in route:
                matched[load_id] = load
    if not matched:
        return 0

    # 5) Total packages por load (HIGH_LEVEL_LOGICAL_COUNT / COUNT).
    load_id_list = [
        {
            "loadId": load_id,
            "isRolledOver": bool(load.get("isRolledOver")),
            "actualUnloadStartTime": load.get("actualUnloadStartTime"),
            "aat": load.get("aat"),
            "sat": load.get("sat"),
            "status": load.get("status") or "SCHEDULED",
            "displayId": load.get("displayId") or "",
        }
        for load_id, load in matched.items()
    ]
    counts_out = _vista_call(
        session, csrf, "getInboundCounts",
        {
            "nodeId": site,
            "searchTime": now_ms,
            "entity": "getInboundCounts",
            "testmode": False,
            "sortPlanDuration": 0,
            "segmentToMeasureTypeList": {
                "segmentId": "HIGH_LEVEL_LOGICAL_COUNT",
                "measureType": "COUNT",
            },
            "containerTypeFilter": ["PACKAGE"],
            "loadIdList": load_id_list,
        },
        timeout=timeout,
    )
    details = (
        (counts_out.get("getInboundCountsOutput") or {}).get("loadLevelDetailList") or []
    )
    total = 0
    for detail in details:
        if str((detail or {}).get("loadId") or "") in matched:
            total += _sum_counts(detail)
    return total


def login_status(kind: str, site: str = "", *, timeout: float = 12.0) -> dict[str, Any]:
    """Lightweight auth probe for the configured Rodeo or Vista source."""
    source = str(kind or "").strip().lower()
    if source == "fc":
        url = RODEO_URL_TEMPLATE.format(site=str(site or "").strip().upper())
        try:
            cookies = _rodeo_cookies()
            if not cookies.get("amzn_sso_token"):
                return {"ready": False, "login_url": url}
            response = requests.get(
                url,
                cookies=cookies,
                headers={"User-Agent": _UA},
                timeout=timeout,
                allow_redirects=True,
            )
            return {
                "ready": response.status_code == 200 and not _looks_like_login(response),
                "login_url": url,
            }
        except Exception:  # noqa: BLE001 - auth probe is best-effort
            return {"ready": False, "login_url": url}

    url = VISTA_URL
    session = requests.Session()
    try:
        session.cookies.update(_vista_cookies())
        response = session.get(
            url,
            headers={"User-Agent": _UA},
            timeout=timeout,
            allow_redirects=True,
        )
        has_csrf = bool(
            re.search(r'<meta content="[^"]+" name="csrf-token"', response.text or "")
            or re.search(r'name="csrf-token"\s+content="[^"]+"', response.text or "")
        )
        return {
            "ready": response.status_code == 200 and not _looks_like_login(response) and has_csrf,
            "login_url": url,
        }
    except Exception:  # noqa: BLE001 - auth probe is best-effort
        return {"ready": False, "login_url": url}
    finally:
        session.close()


__all__ = [
    "TrucksReviewError",
    "TrucksReviewAuthError",
    "fetch_fc_volume",
    "fetch_sc_volume",
    "login_status",
    "RODEO_URL_TEMPLATE",
    "VISTA_URL",
]
