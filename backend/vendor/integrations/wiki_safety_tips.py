"""Safety Tips desde la wiki interna (w.amazon.com) con traducción.

Fuentes (en orden de preferencia):

1. ``https://w.amazon.com/bin/view/Whs/AMZL-EU/MeuwhsAMZL/sftytipps`` —
   "Safety Tips Toolkit" de MEU WHS AMZL: tablas por categoría con los
   tips de ejemplo como bullets ``<li>`` (texto en inglés).
2. ``https://w.amazon.com/bin/view/EDI4/CR/ProblemSolve/ST/List`` —
   "Safety Tip List" de EDI4: cards de imágenes; solo los títulos
   (``<h3 class="title-m">``) son texto utilizable, por eso es fallback.

La wiki autentica vía Midway. Reutilizamos la sesión del operador en
Firefox (patrón estándar de la app: cookies ``amzn_sso_token`` leídas del
perfil, ver :mod:`app.integrations.firefox_cookies`).

La traducción usa el cliente Nova central (:mod:`app.integrations.nova_ai`)
cuando ``NOVA_API_KEY`` está configurada; si no, se devuelve el tip original
en inglés con ``translated: False``.
"""
from __future__ import annotations

import html as _html
import logging
import random
import re
import time
from datetime import date

import requests

from app.integrations.firefox_cookies import read_cookies_from_profile

logger = logging.getLogger(__name__)

WIKI_HOST = "w.amazon.com"
LOGIN_URL = "https://w.amazon.com/"
PRIMARY_URL = "https://w.amazon.com/bin/view/Whs/AMZL-EU/MeuwhsAMZL/sftytipps"
FALLBACK_URL = "https://w.amazon.com/bin/view/EDI4/CR/ProblemSolve/ST/List"

_WIKI_HOST_PATTERNS = (WIKI_HOST, "." + WIKI_HOST, ".amazon.com")
_TIMEOUT = 25.0

# Cache in-process de los tips parseados (la página cambia poco).
_CACHE_TTL_SECONDS = 6 * 3600.0
_cache: dict[str, object] = {"ts": 0.0, "tips": [], "source": ""}

_LOCALE_NAMES = {
    "es": "Spanish",
    "en": "English",
    "fr": "French",
    "de": "German",
    "it": "Italian",
}


class WikiAuthError(RuntimeError):
    """La sesión Midway de Firefox no sirve para w.amazon.com."""


class WikiFetchError(RuntimeError):
    """La wiki no devolvió una página utilizable."""


def _wiki_cookies() -> dict[str, str]:
    return read_cookies_from_profile(_WIKI_HOST_PATTERNS, target_host=WIKI_HOST)


def _fetch_page(url: str) -> str:
    try:
        cookies = _wiki_cookies()
    except RuntimeError as exc:
        raise WikiAuthError(str(exc)) from exc
    if not cookies.get("amzn_sso_token"):
        raise WikiAuthError(
            "Falta la cookie Midway (amzn_sso_token) para w.amazon.com. "
            "Abre la wiki en Firefox e inicia sesión."
        )
    session = requests.Session()
    session.cookies.update(cookies)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
            "Gecko/20100101 Firefox/124.0"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        response = session.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise WikiFetchError(f"Error de red hacia la wiki: {exc}") from exc

    if response.status_code in (401, 403):
        raise WikiAuthError(f"La wiki rechazó la petición ({response.status_code}).")
    if 300 <= response.status_code < 400:
        location = response.headers.get("Location", "")
        raise WikiAuthError(f"La wiki redirigió a sign-in ({location or 'Midway'}).")
    if response.status_code != 200:
        raise WikiFetchError(f"La wiki devolvió {response.status_code}.")

    text = response.text or ""
    low = text.lower()
    if "midway" in low and ("sign in" in low or "sentry" in low) and "safety" not in low:
        raise WikiAuthError("La wiki devolvió la página de login de Midway.")
    return text


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _main_content(page: str) -> str:
    """Recorta el HTML al contenido principal de XWiki (evita menús y TOC)."""
    marker = re.search(r'id="xwikicontent"', page)
    return page[marker.start():] if marker else page


def _parse_primary(page: str) -> list[str]:
    """Bullets ``<li>`` del Safety Tips Toolkit (texto de tips en inglés)."""
    page = _main_content(page)
    tips: list[str] = []
    seen: set[str] = set()
    for match in re.findall(r"<li[^>]*>(.*?)</li>", page, flags=re.S | re.I):
        # Los items del TOC son anchors internos (href="#H...") — no son tips.
        if re.search(r'href="#', match):
            continue
        text = _strip_tags(match)
        # Filtra navegación / items demasiado cortos o largos para ser un tip.
        if not (25 <= len(text) <= 300):
            continue
        # Los tips del toolkit son frases; la navegación de la wiki no lleva punto
        # final ni imperativos. Con la longitud + dedupe es suficiente filtro.
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        tips.append(text)
    return tips


def _parse_fallback(page: str) -> list[str]:
    """Títulos de las cards del Safety Tip List de EDI4 (frases cortas)."""
    tips: list[str] = []
    seen: set[str] = set()
    for match in re.findall(r'<h3 class="title-m"[^>]*>(.*?)</h3>', page, flags=re.S | re.I):
        text = _strip_tags(match)
        if not text or len(text) < 4:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        tips.append(text)
    return tips


def _load_tips(*, force: bool = False) -> tuple[list[str], str]:
    """Devuelve ``(tips, source_url)`` con caché in-process."""
    now = time.monotonic()
    if not force and _cache["tips"] and now - float(_cache["ts"]) < _CACHE_TTL_SECONDS:
        return list(_cache["tips"]), str(_cache["source"])  # type: ignore[arg-type]

    tips: list[str] = []
    source = ""
    primary_error: Exception | None = None
    try:
        tips = _parse_primary(_fetch_page(PRIMARY_URL))
        source = PRIMARY_URL
    except WikiAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 — probamos el fallback
        primary_error = exc
        logger.warning("Safety tips: fuente primaria falló: %s", exc)

    if not tips:
        try:
            tips = _parse_fallback(_fetch_page(FALLBACK_URL))
            source = FALLBACK_URL
        except WikiAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Safety tips: fuente fallback falló: %s", exc)

    if not tips:
        raise WikiFetchError(
            f"No se pudieron extraer safety tips de la wiki. {primary_error or ''}".strip()
        )

    _cache.update({"ts": now, "tips": list(tips), "source": source})
    return tips, source


def _translate(tip: str, locale: str) -> tuple[str, bool]:
    """Traduce el tip con Nova. Devuelve ``(texto, translated)``."""
    target = _LOCALE_NAMES.get((locale or "es").lower()[:2], "Spanish")
    if target == "English":
        return tip, True
    try:
        from app.integrations import nova_ai

        if not nova_ai.is_configured():
            return tip, False
        translated = nova_ai.nova_chat(
            f"Translate the following workplace safety tip to {target}. "
            f"Reply with ONLY the translated sentence, nothing else.\n\n{tip}",
            system_message=(
                "You are a professional translator for Amazon logistics "
                "workplace safety content. Keep the tone direct and clear."
            ),
        )
        translated = translated.strip().strip('"')
        return (translated, True) if translated else (tip, False)
    except Exception:  # noqa: BLE001 — la traducción nunca rompe el fetch
        logger.exception("Safety tips: la traducción falló")
        return tip, False


def get_safety_tip(
    *,
    locale: str = "es",
    when: date | None = None,
    shuffle: bool = False,
) -> dict[str, object]:
    """Devuelve un safety tip de la wiki, traducido al ``locale``.

    Por defecto el tip es determinista por día (mismo tip durante todo el
    día para toda la estación). Con ``shuffle=True`` se elige otro al azar
    (el botón "otro tip" de la UI).
    """
    tips, source = _load_tips()
    if shuffle and len(tips) > 1:
        original = random.choice(tips)
    else:
        idx = (when or date.today()).toordinal() % len(tips)
        original = tips[idx]
    text, translated = _translate(original, locale)
    return {
        "tip": text,
        "original": original,
        "translated": translated,
        "source": source,
        "count": len(tips),
    }


__all__ = [
    "LOGIN_URL",
    "WikiAuthError",
    "WikiFetchError",
    "get_safety_tip",
]
