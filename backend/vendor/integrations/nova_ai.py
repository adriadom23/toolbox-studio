"""Centralised Nova AI (OpenAI-compatible) client.

Daily and DPR both call Nova. Having one module keeps the env reading,
payload shape, model selection and error handling in a single place so
any future tool (Wave, Flow meeting...) just imports ``nova_chat`` or
``nova_chat_with_file``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://api.nova.amazon.com/v1"
DEFAULT_MODEL = "nova-pro-v1"
DEFAULT_TIMEOUT_SEC = 90

# UI locale (navbar) -> language name used in reply-language directives.
# Keep in sync with SUPPORTED_LOCALES in messages_i18n.
LANGUAGE_NAMES = {
    "es": "Spanish",
    "en": "English",
    "fr": "French",
    "de": "German",
    "it": "Italian",
}


def language_name(locale: str | None) -> str:
    """Human language name for a UI locale code (defaults to Spanish)."""
    return LANGUAGE_NAMES.get((locale or "").strip().lower(), "Spanish")


def reply_language_directive(locale: str | None, *, json_output: bool = False) -> str:
    """One-line instruction forcing the reply language of a Nova call.

    Appended to (or embedded in) the system message by ``nova_chat`` /
    ``nova_chat_with_file`` when the caller passes ``reply_locale``. The
    prompts themselves stay in a single canonical language; only this
    directive varies. It explicitly overrides any earlier language hint
    so legacy prompt wording cannot fight it.
    """
    lang = language_name(locale)
    directive = (
        f"IMPORTANT: Write every user-facing sentence of your reply in {lang}. "
        "This overrides any previous language instruction."
    )
    if json_output:
        directive += (
            " Keep the JSON keys, structure and enumerated values "
            "(e.g. HIGH|MEDIUM|LOW) exactly as specified — localize only "
            "the free-text values."
        )
    return directive


class NovaNotConfiguredError(RuntimeError):
    pass


class NovaApiError(RuntimeError):
    pass


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _int_env(key: str, default: int) -> int:
    try:
        return int(float(os.environ.get(key, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _clean_api_key(raw: str) -> str:
    """Normalise a NOVA_API_KEY value pasted with extra shell syntax.

    Operators sometimes paste the whole line from their notes into the
    config workbook cell — e.g. ``export NOVA_API_KEY=<uuid>`` or a quoted
    value. Nova then rejects the literal string with an empty 403. Accept
    those shapes by keeping only the part after the last ``=`` and
    stripping quotes/whitespace.
    """
    value = (raw or "").strip()
    if "=" in value and re.search(r"(?i)\bNOVA_API_KEY\s*=", value):
        value = value.split("=", 1)[1].strip()
    return value.strip("'\"").strip()


def get_api_key() -> str:
    return _clean_api_key(os.environ.get("NOVA_API_KEY", ""))


def is_configured() -> bool:
    return bool(get_api_key())


def _extract_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or "").strip())
            elif isinstance(item, str):
                parts.append(item.strip())
        return "\n".join(p for p in parts if p).strip()
    return str(content or "").strip()


def _build_headers() -> dict[str, str]:
    api_key = get_api_key()
    if not api_key:
        raise NovaNotConfiguredError("Nova no esta configurado. Agrega NOVA_API_KEY en el .env.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Python 3.14 ships stdlib zstd, so urllib3 advertises it in
        # Accept-Encoding by default. Nova's responses then come back
        # zstd-compressed and the decode fails mid-stream with
        # "Zstandard data is incomplete". Pin to gzip/deflate to avoid it.
        "Accept-Encoding": "gzip, deflate",
    }


def _error_detail(response: requests.Response) -> str:
    """A short, human-readable error detail (never a raw HTML error page)."""
    text = (response.text or "").strip()
    if text.lower().startswith("<html") or text.lower().startswith("<!doctype"):
        return "(pagina de error HTML de Amazon)"
    return text[:500]


def _post_chat(payload: dict[str, Any], timeout_s: int) -> dict[str, Any]:
    """POST to Nova's chat/completions with retries on transient failures.

    Nova occasionally returns 5xx (gateway HTML error pages) and 429
    rate limits. Both are worth a couple of blind retries before we
    surface an error to the operator. Non-retryable statuses (4xx auth
    or payload problems) raise immediately.
    """
    base_url = os.environ.get("NOVA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    max_retries = _int_env("NOVA_TRANSIENT_MAX_RETRIES", 3)
    sleep_s = _float_env("NOVA_TRANSIENT_SLEEP_SEC", 4.0)

    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=_build_headers(),
                data=json.dumps(payload),
                timeout=timeout_s,
            )
        except requests.RequestException as exc:
            last_error = f"Error de red hacia Nova: {exc}"
            logger.warning("nova request failed (attempt %d/%d): %s", attempt, max_retries, exc)
        else:
            if response.ok:
                return response.json()
            last_error = f"Nova API error {response.status_code}: {_error_detail(response)}"
            if response.status_code not in (429, 500, 502, 503, 504):
                raise NovaApiError(last_error)
            logger.warning(
                "nova returned %s (attempt %d/%d)", response.status_code, attempt, max_retries
            )
        if attempt < max_retries:
            time.sleep(sleep_s * attempt)
    raise NovaApiError(last_error or "Nova no respondio.")


def _common_payload(model: str) -> dict[str, Any]:
    reasoning_effort = os.environ.get("NOVA_REASONING_EFFORT", "").lower()
    payload: dict[str, Any] = {
        "model": model,
        "temperature": _float_env("NOVA_TEMPERATURE", 0.0),
        "max_tokens": _int_env("NOVA_MAX_TOKENS", 2400),
    }
    if model.lower().startswith("nova-2-lite") and reasoning_effort in {"low", "medium", "high"}:
        payload["reasoning_effort"] = reasoning_effort
    return payload


def nova_chat(
    prompt: str,
    *,
    system_message: str = (
        "You are a Senior Amazon Delivery Operations Area Manager. "
        "Reply in English. Be direct, actionable, and ownership-oriented."
    ),
    model: str | None = None,
    reply_locale: str | None = None,
) -> str:
    """Plain text chat completion against Nova. Returns the reply text.

    ``reply_locale`` (a UI locale code: es/en/fr/de/it) appends a
    reply-language directive to the system message so operator-facing
    output follows the toolbox language. Leave it ``None`` for outputs
    with a fixed language (shared Slack channels, parsed replies).
    """
    if reply_locale:
        system_message = f"{system_message}\n{reply_language_directive(reply_locale)}"
    model = model or os.environ.get("NOVA_MODEL", DEFAULT_MODEL)
    payload = _common_payload(model)
    payload["messages"] = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": prompt},
    ]
    timeout_s = _int_env("NOVA_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
    data = _post_chat(payload, timeout_s)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    text = _extract_content(content)
    if not text:
        raise NovaApiError("Nova devolvio una respuesta vacia.")
    return text


def nova_chat_with_images(
    prompt: str,
    image_urls: list[str],
    *,
    system_message: str = "",
    model: str | None = None,
) -> str:
    """Send a prompt plus one or more image URLs to a Nova vision model.

    Mirrors the legacy ``pod_checker_ai.validate_pod_images_batch`` call:
    the model receives the text instructions followed by ``image_url``
    content blocks (the Honeypot presigned S3 URLs) and returns the raw
    reply text, which the caller parses line by line.
    """
    model = model or os.environ.get("NOVA_MODEL_VISION", os.environ.get("NOVA_MODEL_FILE", "nova-2-lite-v1"))
    payload = _common_payload(model)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for url in image_urls:
        clean = str(url or "").strip()
        if clean:
            content.append({"type": "image_url", "image_url": {"url": clean}})
    messages: list[dict[str, Any]] = []
    if system_message:
        messages.append({"role": "system", "content": system_message})
    messages.append({"role": "user", "content": content})
    payload["messages"] = messages
    timeout_s = _int_env("NOVA_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
    data = _post_chat(payload, timeout_s)
    content_out = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _extract_content(content_out)


def _try_parse_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def nova_chat_with_file(
    file_bytes: bytes,
    filename: str,
    prompt: str,
    *,
    system_message: str = (
        "Eres un SMDO/AMDO de Amazon Delivery Station. "
        "Analiza metricas D-1 por DSP y produce insights accionables. "
        "Devuelve SOLO JSON valido (sin markdown, sin texto extra). "
        "Responde siempre en espanol."
    ),
    model: str | None = None,
    expect_json: bool = True,
    reply_locale: str | None = None,
) -> dict | str:
    """Send a file (typically a CSV) + a prompt. Returns the parsed JSON
    response if ``expect_json`` is True, else the raw string. Mirrors the
    legacy ``nova_chat_completions`` call used in Daily.py.

    ``reply_locale`` appends a reply-language directive (keys/enums stay
    canonical, free text follows the toolbox language). Callers with a
    fixed-language output (e.g. Daily's Slack body) leave it ``None`` and
    keep the default Spanish behaviour.
    """
    if reply_locale:
        system_message = (
            f"{system_message}\n"
            f"{reply_language_directive(reply_locale, json_output=expect_json)}"
        )
    model = model or os.environ.get("NOVA_MODEL", os.environ.get("NOVA_MODEL_FILE", "nova-2-lite-v1"))
    payload = _common_payload(model)
    b64 = base64.b64encode(file_bytes).decode("ascii")
    payload["messages"] = [
        {"role": "system", "content": system_message},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "file", "file": {"filename": filename, "file_data": b64}},
            ],
        },
    ]
    timeout_s = _int_env("NOVA_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
    data = _post_chat(payload, timeout_s)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    text = _extract_content(content)
    if expect_json:
        # Try parsing direct, then strip code fences if Nova wraps it.
        parsed = _try_parse_json(text)
        if parsed is None:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
            parsed = _try_parse_json(cleaned)
        if isinstance(parsed, dict):
            return parsed
        return {"summary": text}
    return text
