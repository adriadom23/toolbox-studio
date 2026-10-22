"""PROTOTIPO — Cliente del nuevo PerfectMile (Caravan, perfectmile.a2z.com).

El PerfectMile legacy (``perfectmile.amazon.com`` / ``bulk.json``) se apaga
el 15 de octubre de 2026. La nueva web es una SPA ("Caravan") cuyo backend
es una API GraphQL (AppSync):

    POST https://prod.caravan.perfectmile.a2z.com/graphql

La autenticacion NO es la cookie Midway directa: es un **Bearer
access_token de Cognito** (~24 h) obtenido con el code flow OIDC:

1. ``GET https://perfectmile-prod.auth.us-east-1.amazoncognito.com/authorize``
   con las cookies Midway de Firefox. Cognito redirige por Amazon
   Federate -> midway-auth -> de vuelta a Cognito, que finalmente
   redirige a ``https://perfectmile.a2z.com/?code=...``.
2. ``POST /oauth2/token`` (SIN cookies — el WAF rechaza peticiones con
   cookies, confirmado por el precedente interno ATS_ROC_MCP_PORTALS)
   intercambia el ``code`` por el ``access_token``.

Los *metric names* tecnicos son LOS MISMOS que en el legacy (el backend
es el mismo LPH): ``dnr_concessions_only_amzl``,
``conceded_units_only_amzl_wankel``, etc.

Queries utiles validadas contra el schema de Caravan
(PerfectMileCaravanBackendPipelineCDK / schema.graphql):

* ``searchMetrics``            — localizar metricas por nombre.
* ``getBatchScopeTableViewData`` — valores agregados (lo que pinta la
  tabla del dashboard).
* ``getMetricDetailsPageData``  — dimensiones disponibles de la metrica;
  las de ``category == diveDeep`` (p. ej. ``tracking_id``) son las de
  fila a fila.
* ``getDrilldownData`` con ``groupByDimensionType=<dim diveDeep>`` — el
  backend enruta a las particiones de dive deep (S3) y devuelve una fila
  por valor (p. ej. un tracking ID por fila). Es el equivalente del
  ``bulk.json`` legacy.

Este modulo es un prototipo de viabilidad: si el smoke test
(``backend/scripts/pm_caravan_smoke.py``) valida punta a punta, la
migracion real consistira en mapear ``METRIC_CATALOG`` de
``perfectmile_client.py`` sobre estas queries.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Sequence
from urllib.parse import parse_qsl, urlparse

import requests

from app.integrations.firefox_cookies import midway_cookies_from_profile


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CARAVAN_SITE = "https://perfectmile.a2z.com"
CARAVAN_GRAPHQL_URL = "https://prod.caravan.perfectmile.a2z.com/graphql"
COGNITO_BASE_URL = "https://perfectmile-prod.auth.us-east-1.amazoncognito.com"
COGNITO_CLIENT_ID = "2hab942etq8t17cua7dh1qf1a4"
COGNITO_REDIRECT_URI = CARAVAN_SITE

DEFAULT_TIMEOUT = 30.0
MAX_REDIRECT_HOPS = 15

_FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# Cabeceras que espera el WAF de Caravan (Origin/Referer del SPA).
_GRAPHQL_HEADERS = {
    "Content-Type": "application/json",
    "Origin": CARAVAN_SITE,
    "Referer": CARAVAN_SITE + "/",
    "User-Agent": _FIREFOX_UA,
}


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


class CaravanError(RuntimeError):
    """Base de errores del cliente Caravan."""


class CaravanAuthError(CaravanError):
    """No se pudo obtener el token (sesion Midway caducada o ausente)."""


class CaravanRequestError(CaravanError):
    """GraphQL devolvio errores o una respuesta no valida."""


# ---------------------------------------------------------------------------
# Token Cognito (cacheado en memoria, ~24 h)
# ---------------------------------------------------------------------------


_TOKEN_CACHE: dict[str, Any] = {"token": "", "exp": 0}


def _jwt_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "ignore"))
        return int(data.get("exp") or 0)
    except Exception:
        return 0


def _midway_session() -> requests.Session:
    """Session con las cookies Midway de Firefox sobre ``.amazon.com``.

    La cadena de redirects del authorize cruza varios hosts
    (cognito -> idp.federate.amazon.com -> midway-auth.amazon.com ->
    cognito). Sembramos las cookies en ``.amazon.com`` (patron ya usado
    por mercury_client) para que sigan viajando en cada salto; las que
    Cognito/Federate vayan poniendo por el camino las gestiona el jar.
    """
    cookies = midway_cookies_from_profile() or {}
    if not cookies.get("session"):
        raise CaravanAuthError(
            "No hay sesion de Midway en Firefox. Abre https://perfectmile.a2z.com "
            "en Firefox, inicia sesion y reintenta."
        )
    sess = requests.Session()
    for name, value in cookies.items():
        sess.cookies.set(name, value, domain=".amazon.com")
        sess.cookies.set(name, value, domain="midway-auth.amazon.com")
    sess.headers.update({"User-Agent": _FIREFOX_UA, "Accept": "*/*"})
    return sess


def _authorize_code(timeout: float = DEFAULT_TIMEOUT) -> str:
    """Recorre la cadena OIDC y devuelve el ``code`` del redirect final."""
    sess = _midway_session()
    url = (
        f"{COGNITO_BASE_URL}/authorize"
        f"?redirect_uri={COGNITO_REDIRECT_URI}"
        f"&response_type=code&client_id={COGNITO_CLIENT_ID}&state=/"
    )
    for _hop in range(MAX_REDIRECT_HOPS):
        try:
            response = sess.get(url, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise CaravanError(f"Error de red en el flujo de login: {exc}") from exc
        location = response.headers.get("Location") or ""
        target = location or response.url
        # ¿Ya tenemos el code FINAL? Solo cuenta el redirect al
        # redirect_uri (perfectmile.a2z.com). OJO: la cadena contiene un
        # ``code=`` intermedio (Federate -> cognito /oauth2/idpresponse)
        # que NO es canjeable — hay que dejar que la session lo siga.
        parsed_target = urlparse(target)
        query = dict(parse_qsl(parsed_target.query, keep_blank_values=True))
        if query.get("code") and parsed_target.netloc == urlparse(COGNITO_REDIRECT_URI).netloc:
            return query["code"]
        if response.status_code in (301, 302, 303, 307, 308) and location:
            # Location puede ser relativa al host actual.
            if location.startswith("/"):
                parsed = urlparse(url)
                url = f"{parsed.scheme}://{parsed.netloc}{location}"
            else:
                url = location
            continue
        # 200 sin code: normalmente la pagina de login de Midway
        # (sesion caducada) o un formulario de Federate que requiere JS.
        break
    raise CaravanAuthError(
        "El flujo de login de PerfectMile no devolvio un authorization code. "
        "Lo mas probable: sesion Midway caducada. Abre "
        f"{CARAVAN_SITE} en Firefox, inicia sesion y reintenta."
    )


def mint_access_token(*, force: bool = False, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Devuelve un access_token de Cognito (cacheado hasta su caducidad)."""
    now = int(time.time())
    cached = _TOKEN_CACHE.get("token") or ""
    if not force and cached and _TOKEN_CACHE.get("exp", 0) - 60 > now:
        return cached

    code = _authorize_code(timeout=timeout)

    # IMPORTANTE: el exchange va SIN cookies (WAF). requests.post pelado.
    try:
        response = requests.post(
            f"{COGNITO_BASE_URL}/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _FIREFOX_UA,
            },
            data={
                "grant_type": "authorization_code",
                "client_id": COGNITO_CLIENT_ID,
                "code": code,
                "redirect_uri": COGNITO_REDIRECT_URI,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise CaravanError(f"Error de red en el token exchange: {exc}") from exc

    try:
        tokens = response.json()
    except Exception as exc:
        raise CaravanAuthError(
            f"Cognito no devolvio JSON en el token exchange (HTTP {response.status_code})."
        ) from exc
    token = tokens.get("access_token") or ""
    if not token:
        raise CaravanAuthError(
            f"Cognito no devolvio access_token: {json.dumps(tokens)[:300]}"
        )

    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["exp"] = _jwt_exp(token) or (now + int(tokens.get("expires_in") or 3600))
    return token


def login_status() -> dict[str, Any]:
    """Informa de si podemos obtener token (para la barra de conexion)."""
    try:
        token = mint_access_token()
    except CaravanError as exc:
        return {"ready": False, "reason": str(exc), "login_url": CARAVAN_SITE}
    return {"ready": bool(token), "login_url": CARAVAN_SITE}


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------


def gql(
    query: str,
    variables: dict[str, Any] | None = None,
    operation_name: str | None = None,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """POST GraphQL con Bearer token. Reintenta una vez si el token caduca."""
    payload: dict[str, Any] = {"query": query, "variables": variables or {}}
    if operation_name:
        payload["operationName"] = operation_name

    for attempt in (1, 2):
        token = mint_access_token(force=(attempt == 2))
        headers = {**_GRAPHQL_HEADERS, "Authorization": f"Bearer {token}"}
        try:
            # Sin session/cookies a proposito (WAF).
            response = requests.post(
                CARAVAN_GRAPHQL_URL, headers=headers, json=payload, timeout=timeout
            )
        except requests.RequestException as exc:
            raise CaravanError(f"Error de red contra Caravan GraphQL: {exc}") from exc
        try:
            result = response.json()
        except Exception as exc:
            raise CaravanRequestError(
                f"Caravan no devolvio JSON (HTTP {response.status_code}): "
                f"{(response.text or '')[:300]}"
            ) from exc
        errors = result.get("errors") or []
        if errors:
            error_type = str((errors[0] or {}).get("errorType") or "")
            if attempt == 1 and "Unauthorized" in error_type:
                _TOKEN_CACHE["token"] = ""
                continue
            raise CaravanRequestError(f"GraphQL errors: {json.dumps(errors)[:500]}")
        return result.get("data") or {}
    raise CaravanRequestError("GraphQL: sin respuesta tras reintento.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Helpers de alto nivel (los que usara la migracion del METRIC_CATALOG)
# ---------------------------------------------------------------------------


def search_metrics(term: str, *, business: str | None = None) -> list[dict[str, Any]]:
    data = gql(
        "query searchMetrics($input: String!, $business: String) {"
        " searchMetrics(input: $input, business: $business) {"
        " hits { id name unitsOfMeasurement } total } }",
        {"input": term, "business": business},
        "searchMetrics",
    )
    return list(((data.get("searchMetrics") or {}).get("hits")) or [])


def get_metric_value(
    metric_name: str,
    date_start: str,
    date_end: str | None = None,
    *,
    station: str | None = None,
    country: str | None = None,
    region: str = "EU",
    periodicity: str = "DAILY",
) -> list[dict[str, Any]]:
    """Valor agregado de la metrica (equivale a la celda del dashboard)."""
    if station:
        dimensionality = json.dumps({"location": [station]})
    elif country:
        dimensionality = json.dumps({"country": [country]})
    else:
        dimensionality = json.dumps({"region": [region]})
    data = gql(
        "query getBatchScopeTableViewData($requests: [ScopeTableViewRequest]!) {"
        " getBatchScopeTableViewData(requests: $requests) {"
        " responses { dimensionValue date dimensionType"
        " metricData { metricValue { quantity value } } } } }",
        {
            "requests": [
                {
                    "metricName": metric_name,
                    "startDate": date_start,
                    "endDate": date_end or date_start,
                    "periodicity": periodicity,
                    "dimensionality": dimensionality,
                    "primaryRegion": region,
                    "metricTargetsV2Flag": True,
                    "country": country or "",
                }
            ]
        },
        "getBatchScopeTableViewData",
    )
    return list(((data.get("getBatchScopeTableViewData") or {}).get("responses")) or [])


def get_metrics_table(
    metric_names: Sequence[str],
    date_start: str,
    date_end: str,
    *,
    station: str,
    country: str | None = None,
    region: str = "EU",
    periodicity: str = "DAILY",
    detail_dimension: str = "dsp",
    extra_dimensions: dict[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Fetch scope totals and one drilldown through Caravan's export resolver.

    ``getBatchExportData`` is the same resolver used by the dashboard's
    download action. It retrieves scope and drilldown data together and
    returns one status per metric for each level. Callers may use a date range,
    but Daily Meeting intentionally invokes this function once per exact day
    so D-2/D-3 can cross an Amazon-week boundary safely.
    """
    dimensions: dict[str, list[str]] = {"location": [station]}
    for key, values in (extra_dimensions or {}).items():
        dimensions[str(key)] = [str(value) for value in values]

    metric_list = [str(name) for name in metric_names]
    scope_responses: list[Any] = []
    scope_statuses: list[Any] = []
    expanded_responses: list[Any] = []
    expanded_statuses: list[Any] = []

    # Mirror Caravan's official export client. It caps each resolver call at
    # ten metrics to stay below AppSync's response-size limit, then merges the
    # chunks while preserving the original metric order.
    metrics_per_chunk = 10
    for start in range(0, len(metric_list), metrics_per_chunk):
        chunk = metric_list[start : start + metrics_per_chunk]
        data = gql(
            "query getDailyMetricTable($input: BatchExportRequest!) {"
            " export: getBatchExportData(input: $input) {"
            " scopeResponses { dimensionValue date dimensionType dimensionality periodicity"
            " metricData { metricValue { quantity value ponderation updatedAt }"
            " metricTarget { type value secondTargetValue padding } } }"
            " drilldownResponses { dimensionName responses {"
            " dimensionValue date dimensionType dimensionality periodicity"
            " metricData { metricValue { quantity value ponderation updatedAt }"
            " metricTarget { type value secondTargetValue padding } } } }"
            " statuses { status errors { errorMessage fieldName } } } }",
            {
                "input": {
                    "metricNames": chunk,
                    "drilldownDimensions": [detail_dimension],
                    "startDate": date_start,
                    "endDate": date_end,
                    "periodicity": periodicity,
                    "country": country or "",
                    "primaryRegion": region,
                    "dimensionality": json.dumps(dimensions),
                    "metricTargetsV2Flag": True,
                }
            },
            "getDailyMetricTable",
        )
        export = data.get("export") or {}
        drilldown_levels = export.get("drilldownResponses") or []
        drilldown = next(
            (
                level
                for level in drilldown_levels
                if isinstance(level, dict)
                and str(level.get("dimensionName") or "") == detail_dimension
            ),
            {},
        )
        statuses = list(export.get("statuses") or [])
        chunk_size = len(chunk)
        chunk_scope = list(export.get("scopeResponses") or [])
        chunk_expanded = list(drilldown.get("responses") or [])
        scope_responses.extend(
            chunk_scope[index] if index < len(chunk_scope) else []
            for index in range(chunk_size)
        )
        expanded_responses.extend(
            chunk_expanded[index] if index < len(chunk_expanded) else []
            for index in range(chunk_size)
        )
        scope_statuses.extend(
            statuses[index] if index < len(statuses) else {}
            for index in range(chunk_size)
        )
        expanded_statuses.extend(
            statuses[chunk_size + index]
            if chunk_size + index < len(statuses)
            else {}
            for index in range(chunk_size)
        )

    return {
        "scope_responses": scope_responses,
        "scope_statuses": scope_statuses,
        "expanded_responses": expanded_responses,
        "expanded_statuses": expanded_statuses,
    }


def get_metric_dimensions(
    metric_name: str,
    *,
    region: str = "EU",
) -> dict[str, Any]:
    """Info de dimensiones de la metrica: ``metricDimensions`` + ``partitions``.

    Las dimensiones con ``category == 'diveDeep'`` (p. ej. ``tracking_id``)
    son las de fila a fila.
    """
    data = gql(
        "query getMetricDimensionInfo($metricName: String!, $primaryRegion: Region!) {"
        " getMetricDimensionInfo(metricName: $metricName, primaryRegion: $primaryRegion) {"
        " metricDimensions { category displayName technicalName }"
        " partitions } }",
        {"metricName": metric_name, "primaryRegion": region},
        "getMetricDimensionInfo",
    )
    return data.get("getMetricDimensionInfo") or {}


def fetch_rows(
    metric_name: str,
    date_start: str,
    date_end: str | None = None,
    *,
    row_dim: str = "tracking_id",
    attr_dims: Sequence[str] = (),
    station: str | None = None,
    country: str | None = None,
    region: str = "EU",
    periodicity: str = "DAILY",
    extra_filters: dict[str, Sequence[str]] | None = None,
    progress_cb: Any | None = None,
) -> list[dict[str, Any]]:
    """Reconstruye filas multi-columna estilo ``bulk.json`` legacy.

    Caravan solo devuelve UNA dimension por query (``getDrilldownData``),
    asi que pivotamos: para cada ``attr_dim`` se enumeran sus valores y se
    re-consulta la lista de ``row_dim`` filtrada por cada valor. Validado
    con cobertura 100% (p. ej. DNR: tracking x bucket_l3 x dsp, 69/69).

    Coste: por dia, ``1 + sum(1 + V_attr)`` queries — usable solo con
    atributos de cardinalidad baja (buckets, DSPs, estados...). NO usar
    con atributos de cardinalidad alta (datetimes, order ids, zips).
    Las queries por valor se lanzan en paralelo (pool de hilos) para que
    una semana de DNR quede en ~20-30 s en vez de minutos.

    Devuelve una fila por valor de ``row_dim`` y dia:
    ``{row_dim: <id>, <attr>: <valor>..., "row_count": n, "fetch_date": dia}``.
    """
    import datetime as _dt
    from concurrent.futures import ThreadPoolExecutor

    start = _dt.date.fromisoformat(date_start[:10])
    end = _dt.date.fromisoformat((date_end or date_start)[:10])
    if end < start:
        raise CaravanRequestError("La fecha final no puede ser anterior a la inicial.")

    def _emit(stage: str, current: int, total: int, detail: str = "") -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(stage, current, total, detail)
        except Exception:
            pass

    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += _dt.timedelta(days=1)

    out: list[dict[str, Any]] = []
    for idx, day in enumerate(days, start=1):
        _emit("day", idx, len(days), day)
        base = get_dive_deep_rows(
            metric_name,
            day,
            row_dim,
            station=station,
            country=country,
            region=region,
            periodicity=periodicity,
            extra_filters=extra_filters,
        )
        rows_by_id: dict[str, dict[str, Any]] = {}
        for entry in base:
            rid = str(entry.get("dimensionValue") or "").strip()
            if not rid:
                continue
            rows_by_id[rid] = {
                row_dim: rid,
                "row_count": entry.get("metricValue"),
                "fetch_date": day,
            }
        if not rows_by_id:
            continue
        # Enumerar los valores de cada atributo (secuencial: son pocas
        # queries) y despues resolver TODOS los pares (attr, valor) en
        # paralelo — cada una es una query independiente de solo lectura.
        pairs: list[tuple[str, str]] = []
        for attr in attr_dims:
            try:
                values = get_dive_deep_rows(
                    metric_name,
                    day,
                    attr,
                    station=station,
                    country=country,
                    region=region,
                    periodicity=periodicity,
                    extra_filters=extra_filters,
                )
            except CaravanRequestError:
                # Atributo no disponible para la metrica: fila sin columna.
                continue
            for value_entry in values:
                value = str(value_entry.get("dimensionValue") or "").strip()
                if not value:
                    # Los valores vacios no se pueden filtrar server-side;
                    # esas filas quedan sin el atributo (igual que legacy
                    # con la celda en blanco).
                    continue
                pairs.append((attr, value))

        def _resolve(pair: tuple[str, str]) -> tuple[str, str, list[dict[str, Any]]]:
            attr, value = pair
            merged = {**(extra_filters or {}), attr: [value]}
            sub = get_dive_deep_rows(
                metric_name,
                day,
                row_dim,
                station=station,
                country=country,
                region=region,
                periodicity=periodicity,
                extra_filters=merged,
            )
            return attr, value, sub

        if pairs:
            with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as pool:
                for attr, value, sub in pool.map(_resolve, pairs):
                    for entry in sub:
                        rid = str(entry.get("dimensionValue") or "").strip()
                        if rid in rows_by_id:
                            rows_by_id[rid][attr] = value
        out.extend(rows_by_id.values())
    return out


def get_dive_deep_rows(
    metric_name: str,
    date: str,
    dive_deep_dimension: str,
    *,
    station: str | None = None,
    country: str | None = None,
    region: str = "EU",
    periodicity: str = "DAILY",
    extra_filters: dict[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """Filas de dive deep: una entrada por valor de la dimension.

    Con ``dive_deep_dimension='tracking_id'`` devuelve un tracking ID por
    fila (dimensionValue) con su contador (metricValue) — el equivalente
    funcional del ``bulk.json`` legacy para el caso de uso del ToolBox.
    """
    dims: dict[str, Any] = {}
    if station:
        dims["location"] = [station]
    if country:
        dims["country"] = [country]
    for key, values in (extra_filters or {}).items():
        dims[key] = list(values)
    data = gql(
        "query getDrilldownData($metricName: String!, $country: String, $date: String!,"
        " $dimensionality: String!, $periodicity: String!, $groupByDimensionType: String!,"
        " $primaryRegion: Region!, $secondaryFilterIn: String, $linkedDimensionType: String,"
        " $metricTargetsV2Flag: Boolean!) {"
        " getDrilldownData(metricName: $metricName, country: $country, date: $date,"
        " dimensionality: $dimensionality, periodicity: $periodicity,"
        " groupByDimensionType: $groupByDimensionType, primaryRegion: $primaryRegion,"
        " secondaryFilterIn: $secondaryFilterIn, linkedDimensionType: $linkedDimensionType,"
        " metricTargetsV2Flag: $metricTargetsV2Flag) {"
        " values { dimensionValue metricValue } } }",
        {
            "metricName": metric_name,
            "country": country,
            "date": date,
            "dimensionality": json.dumps(dims),
            "periodicity": periodicity,
            "groupByDimensionType": dive_deep_dimension,
            "primaryRegion": region,
            "secondaryFilterIn": None,
            "linkedDimensionType": None,
            "metricTargetsV2Flag": True,
        },
        "getDrilldownData",
    )
    return list(((data.get("getDrilldownData") or {}).get("values")) or [])
