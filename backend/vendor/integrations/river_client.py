"""River (river.amazon.com) workflow client — creación de TTs sin navegador.

River es el portal de helpfinder/workflows que crea los TTs de estación. Su
"formulario" es un player de pasos: cada paso es un ``POST /workflow/execute``
que devuelve el HTML del paso siguiente más un ``executionId`` y el
``contextData`` acumulado. Este módulo replica ese flujo server-side
reutilizando las cookies Midway del Firefox del operador (mismo patrón que
:mod:`cortex_client`, :mod:`perfectmile_client`, etc.), de forma que el
ToolBox puede:

1. Abrir un workflow por deep-link (workflowId + nodos q0..q4).
2. Renderizar cada paso DENTRO del ToolBox (campos parseados del HTML).
3. Avanzar con los valores que el operador revisa/edita.
4. Enviar el TT solo cuando el operador confirma el último paso.

El HTML de los pasos posiciona todo en absoluto (spans de texto e inputs con
``style="left:..px;top:..px"``), así que la asociación etiqueta→campo se hace
por geometría. Los campos se identifican por su ``ng-model`` (``data.xxx``),
que es lo que espera el endpoint como clave de variable.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from app.integrations.firefox_cookies import read_cookies_from_profile

logger = logging.getLogger(__name__)

RIVER_HOST = "river.amazon.com"
RIVER_BASE = f"https://{RIVER_HOST}"
RIVER_LOGIN_URL = f"{RIVER_BASE}/?org=last_mile"

RIVER_HOST_PATTERNS = (
    RIVER_HOST,
    f".{RIVER_HOST}",
    ".amazon.com",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# Ejecuciones en curso: execution_id → estado necesario para el paso siguiente.
# ToolBox es una app local mono-operador; un dict con TTL es suficiente.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_TTL_S = 3600


class RiverAuthError(RuntimeError):
    """Sesión Midway de River ausente o caducada."""


class RiverError(RuntimeError):
    """Fallo de la API de River (respuesta inesperada)."""


# ---------------------------------------------------------------------------
# Sesión HTTP
# ---------------------------------------------------------------------------


def _cookies() -> dict[str, str]:
    return read_cookies_from_profile(
        RIVER_HOST_PATTERNS,
        target_host=RIVER_HOST,
    )


def _session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": RIVER_BASE,
            "Referer": f"{RIVER_BASE}/",
        }
    )
    for name, value in _cookies().items():
        sess.cookies.set(name, value, domain=RIVER_HOST)
    return sess


def _looks_like_login(response: requests.Response) -> bool:
    if response.status_code in (301, 302, 401, 403):
        return True
    ctype = response.headers.get("Content-Type", "")
    if "text/html" in ctype and "midway" in response.text[:4000].lower():
        return True
    return False


def status() -> dict[str, Any]:
    """Estado de la sesión de River: ``{ready, user, login_url, error?}``."""
    try:
        sess = _session()
        resp = sess.get(f"{RIVER_BASE}/getUserDetails", timeout=20, allow_redirects=False)
        if _looks_like_login(resp):
            return {"ready": False, "login_url": RIVER_LOGIN_URL}
        data = resp.json()
        user = data.get("userName") or data.get("user") or data.get("login") or ""
        return {"ready": True, "user": user, "login_url": RIVER_LOGIN_URL}
    except ValueError:
        return {"ready": False, "login_url": RIVER_LOGIN_URL}
    except requests.RequestException as exc:
        return {"ready": False, "login_url": RIVER_LOGIN_URL, "error": str(exc)}


# ---------------------------------------------------------------------------
# Parseo del HTML de un paso
# ---------------------------------------------------------------------------

_STYLE_POS_RE = re.compile(r"left:\s*(-?[\d.]+)px.*?top:\s*(-?[\d.]+)px", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = _TAG_RE.sub("", text)
    return unescape(text).replace("\xa0", " ").strip()


def _style_pos(attrs: str) -> tuple[float, float] | None:
    m = _STYLE_POS_RE.search(attrs)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def _parse_select_options(segment: str) -> list[str]:
    """Opciones de un ``<select>`` de River (vienen en el ng-repeat inline)."""
    m = re.search(r'ng-repeat="x in \[(.*?)\]', segment, re.S)
    if not m:
        # <option> estáticos como fallback
        return [
            _strip_tags(o)
            for o in re.findall(r"<option[^>]*>(.*?)</option>", segment, re.S)
            if _strip_tags(o)
        ]
    raw = unescape(m.group(1))
    return [s.strip().strip('"').strip() for s in re.findall(r'"([^"]*)"', raw)]


def parse_step_html(html: str) -> dict[str, Any]:
    """Extrae del HTML de un paso: título, textos visibles y campos.

    Cada campo lleva su ``ng_model`` (clave para /workflow/execute), tipo,
    etiqueta asociada geométricamente, placeholder, opciones y required.
    """
    title_m = re.search(r'<page-info label="([^"]*)"', html)
    title = unescape(title_m.group(1)) if title_m else ""

    # ── Bloques de texto visibles (etiquetas + instrucciones) ──
    labels: list[dict[str, Any]] = []
    texts: list[str] = []
    for m in re.finditer(
        r'<span style="position:absolute[^"]*"[^>]*>(.*?)</span>\s*(?=<(?:span|input|select|textarea|a)\s|$)',
        html,
        re.S,
    ):
        pos = _style_pos(m.group(0))
        text = _strip_tags(m.group(1))
        if not text:
            continue
        if pos and len(text) <= 140:
            labels.append({"text": text, "left": pos[0], "top": pos[1]})
        if len(text) > 140:
            texts.append(text)

    # ── Controles ──
    fields: list[dict[str, Any]] = []
    control_re = re.compile(
        r"<(input|select|textarea)([^>]*ng-model=\"data\.(\w+)\"[^>]*)>", re.S
    )
    for m in control_re.finditer(html):
        tag, attrs, ng_model = m.group(1), m.group(2), m.group(3)
        type_m = re.search(r'type="(\w+)"', attrs)
        ftype = (
            "select"
            if tag == "select"
            else "textarea"
            if tag == "textarea"
            else (type_m.group(1) if type_m else "text")
        )
        ph_m = re.search(r'placeholder="([^"]*)"', attrs)
        pos = _style_pos(attrs)
        options: list[str] = []
        if tag == "select":
            options = _parse_select_options(html[m.end() : m.end() + 2000])

        # Etiqueta más cercana: misma fila a la izquierda, o justo encima.
        label = ""
        if pos:
            best_score = float("inf")
            for cand in labels:
                dy = abs(cand["top"] - pos[1])
                dx = pos[0] - cand["left"]
                if dy < 18 and 0 < dx < 460:
                    score = dx
                elif -40 < pos[1] - cand["top"] < 60 and abs(dx) < 200 and cand["top"] < pos[1]:
                    score = 500 + (pos[1] - cand["top"])
                else:
                    continue
                if score < best_score:
                    best_score = score
                    label = cand["text"]

        fields.append(
            {
                "ng_model": ng_model,
                "type": ftype,
                "label": label,
                "placeholder": unescape(ph_m.group(1)) if ph_m else "",
                "options": options,
                "required": "required" in attrs,
            }
        )

    # Textos informativos: bloques largos (instrucciones tipo checklist).
    info_texts = texts[:6]

    # Enlaces del paso (SOPs, ticket creado...)
    links = []
    for lm in re.finditer(r"openLink\('([^']+)'", html):
        links.append(lm.group(1))
    for lm in re.finditer(r'href="(https?://[^"]+)"', html):
        links.append(lm.group(1))

    return {
        "title": title,
        "fields": fields,
        "texts": info_texts,
        "links": links[:8],
    }


# ---------------------------------------------------------------------------
# Ejecución del workflow
# ---------------------------------------------------------------------------


def _base_body(state: dict[str, Any]) -> dict[str, Any]:
    body = {
        "selectedBusinessOrg": state["org"],
        "buildingId": state["building_id"],
        "country": state["country"],
        "isCorpBuilding": "false",
        "isBuildingKivaEnabled": "false",
        "workflowId": state["workflow_id"],
        "locale": state["locale"],
        "workflowExecutionUrl": state["execution_url"],
    }
    if state.get("workflow_version"):
        body["workflowVersion"] = state["workflow_version"]
    return body


def _post_execute(sess: requests.Session, body: dict[str, Any]) -> dict[str, Any]:
    resp = sess.post(
        f"{RIVER_BASE}/workflow/execute",
        json=body,
        timeout=45,
        allow_redirects=False,
    )
    if _looks_like_login(resp):
        raise RiverAuthError("Sesión de River caducada. Abre River en Firefox e inicia sesión.")
    try:
        data = resp.json()
    except ValueError as exc:
        raise RiverError(
            f"River devolvió una respuesta no-JSON (HTTP {resp.status_code})."
        ) from exc
    if resp.status_code != 200:
        # The raw payload stays in the log; the UI-facing message keeps only
        # the status code.
        logger.warning(
            "river /workflow/execute HTTP %s: %s", resp.status_code, str(data)[:300]
        )
        raise RiverError(f"HTTP {resp.status_code} de /workflow/execute.")
    return data


def _scrape_workflow_version(sess: requests.Session, state: dict[str, Any]) -> str:
    """Última opción: rascar la versión del workflow de la página run."""
    try:
        url = (
            f"{RIVER_BASE}/workflow/{state['building_id']}/run/{state['workflow_id']}/"
            f"?buildingType={state['org']}"
        )
        page = sess.get(url, timeout=30)
        m = re.search(r'"version"\s*:\s*"?(\d+)"?', page.text)
        if m:
            return m.group(1)
    except requests.RequestException:
        pass
    return ""


def _prune_sessions() -> None:
    now = time.time()
    stale = [k for k, v in _SESSIONS.items() if now - v.get("_ts", 0) > _SESSION_TTL_S]
    for key in stale:
        _SESSIONS.pop(key, None)


def _step_payload(state: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    step = parse_step_html(data.get("html", ""))
    return {
        "session_id": state["session_id"],
        "step": step,
        "has_next": bool(data.get("hasNextStep")),
        "has_prev": bool(data.get("hasPrevStep")),
        "has_permission": data.get("hasPermission", True),
        "step_index": state["step_index"],
    }


def start_workflow(
    deep_link: str,
    *,
    building_id: str,
    country: str = "ES",
    locale: str = "es",
) -> dict[str, Any]:
    """Arranca la ejecución de un workflow desde su deep-link.

    Devuelve el primer paso parseado más un ``session_id`` propio para
    encadenar :func:`next_step`.
    """
    _prune_sessions()
    parsed = urlparse(deep_link)
    wf_m = re.search(r"/workflow/(?:[A-Z0-9]+/)?run/(wfss-[\w-]+)", parsed.path)
    if not wf_m:
        raise RiverError(f"Deep-link sin workflowId reconocible: {deep_link}")
    workflow_id = wf_m.group(1)
    qs = parse_qs(parsed.query)
    org = (qs.get("org") or qs.get("buildingType") or ["last_mile"])[0]
    variables: dict[str, Any] = {"buildingType": org}
    for key in ("q0", "q1", "q2", "q3", "q4"):
        variables[key] = (qs.get(key) or [""])[0]

    sess = _session()
    # Validación de sesión + login del operador (útil para el Reported By).
    st = status()
    if not st.get("ready"):
        raise RiverAuthError("No hay sesión de River en Firefox. Abre River e inicia sesión.")

    state: dict[str, Any] = {
        "session_id": uuid.uuid4().hex,
        "workflow_id": workflow_id,
        "workflow_version": "",
        "org": org,
        "building_id": building_id,
        "country": country,
        "locale": locale,
        "execution_url": deep_link.split("#")[0],
        "execution_id": "",
        "context_data": {},
        "step_index": 0,
        "_ts": time.time(),
    }

    body = _base_body(state)
    body["variables"] = variables
    try:
        data = _post_execute(sess, body)
    except RiverError:
        # Reintento con la versión rascada de la página (algunos tenants la exigen).
        version = _scrape_workflow_version(sess, state)
        if not version:
            raise
        state["workflow_version"] = version
        body = _base_body(state)
        body["variables"] = variables
        data = _post_execute(sess, body)

    state["execution_id"] = data.get("executionId", "")
    state["context_data"] = data.get("contextData") or {}
    # La versión real viene en el contextData de la primera respuesta.
    wf_meta = (state["context_data"] or {}).get("workflow") or {}
    if wf_meta.get("version"):
        state["workflow_version"] = str(wf_meta["version"])
    _SESSIONS[state["session_id"]] = state
    return _step_payload(state, data)


def next_step(session_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """Avanza un paso enviando ``values`` (``{ng_model: value}``)."""
    state = _SESSIONS.get(session_id)
    if not state:
        raise RiverError("La ejecución expiró. Vuelve a iniciar el TT.")
    state["_ts"] = time.time()

    sess = _session()
    variables = dict(state["context_data"])
    variables.setdefault("approvalData", None)
    for key, value in (values or {}).items():
        # Solo claves con pinta de ng-model de River; evita inyectar basura.
        if re.fullmatch(r"[A-Za-z][\w]*", str(key) or ""):
            variables[str(key)] = value

    body = _base_body(state)
    body["executionId"] = state["execution_id"]
    body["variables"] = variables
    data = _post_execute(sess, body)

    state["context_data"] = data.get("contextData") or state["context_data"]
    if data.get("executionId"):
        state["execution_id"] = data["executionId"]
    state["step_index"] += 1
    return _step_payload(state, data)


def discard(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)
