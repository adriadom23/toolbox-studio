"""Preferencias LOCALES de tamaño de letra por sección (por usuario/equipo).

Espejo backend de ``frontend/src/shared/fontSizes.js``: el operador elige
en Configuración ▸ Tamaños de letra un porcentaje por sección (cards,
tablas, emails, ...). La UI se aplica sola en el frontend (localStorage +
CSS), pero la sección ``emails`` la necesita el BACKEND porque es quien
compone y envía el HTML — de ahí este fichero.

Persistencia: ``font_prefs.json`` en el directorio de estado runtime
(``TOOLBOX_RUNTIME_STATE_DIR``, el mismo de ``active_station.txt`` /
``operator_login.txt``). Es una preferencia POR MÁQUINA/USUARIO: nunca
toca el workbook de configuración compartido ni SharePoint.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Ids estables, en sincronía con FONT_SIZE_SECTIONS del frontend.
FONT_SECTIONS = (
    "general",
    "navbar",
    "cards",
    "headers",
    "tables",
    "buttons",
    "inputs",
    "popups",
    "emails",
)

# Clamps más laxos que el step de la UI: el fichero puede venir editado a
# mano y cualquier porcentaje razonable debe sobrevivir.
MIN_PCT = 50
MAX_PCT = 250
DEFAULT_PCT = 100


def _font_prefs_file() -> Path:
    """Path del fichero runtime con las escalas (patrón operator_login)."""
    state_dir = os.environ.get("TOOLBOX_RUNTIME_STATE_DIR", "").strip()
    base = Path(state_dir) if state_dir else Path(__file__).resolve().parents[2] / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base / "font_prefs.json"


def clean_scales(raw: object) -> dict[str, int]:
    """Sanea ``{seccion: porcentaje}``: solo secciones conocidas, enteros
    clampados, y SIN entradas en el default (100 = sin override)."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        section = str(key or "").strip().lower()
        if section not in FONT_SECTIONS:
            continue
        try:
            pct = int(round(float(value)))
        except (TypeError, ValueError):
            continue
        pct = max(MIN_PCT, min(MAX_PCT, pct))
        if pct != DEFAULT_PCT:
            out[section] = pct
    return out


def read_font_prefs() -> dict[str, int]:
    """Overrides guardados (``{}`` si no hay fichero o es ilegible)."""
    try:
        raw = json.loads(_font_prefs_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return clean_scales(raw)


def write_font_prefs(scales: object) -> bool:
    """Persiste los overrides saneados. ``False`` si el disco falla."""
    cleaned = clean_scales(scales)
    try:
        _font_prefs_file().write_text(
            json.dumps(cleaned, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except OSError:
        logger.warning("font_prefs: no se pudo escribir %s", _font_prefs_file())
        return False


def email_font_scale() -> float:
    """Factor (1.0 = 100%) para el HTML de los emails enviados."""
    return read_font_prefs().get("emails", DEFAULT_PCT) / 100.0
