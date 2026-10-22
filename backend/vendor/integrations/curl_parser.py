"""Parse a ``curl`` command string into URL + headers + cookies.

Firefox/Chrome DevTools offer a "Copy as cURL" action on any network
request. We parse that string so the backend can replay the exact same
request (same URL, headers, cookies, query parameters) without having
to reimplement the SigV4 / Cognito flow behind Harmony APIs.

The parser is intentionally small and tolerant: it only extracts what
we need (``-H / --header``, ``-b / --cookie``, ``--url``) and tolerates
the common shell quoting styles produced by Firefox (single or double
quotes, trailing backslashes, ``^`` line continuations from Windows
DevTools, etc.).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse


@dataclass
class ParsedCurl:
    url: str = ""
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def url_without_query(self) -> str:
        if not self.url:
            return ""
        parsed = urlparse(self.url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else self.url


_TOKEN_NORMALIZERS: tuple[tuple[str, str], ...] = (
    # Windows CMD style
    ("^\n", " "),
    ("^\r\n", " "),
    ("^\n", " "),
    # Common DevTools line continuations
    ("\\\n", " "),
    ("\\\r\n", " "),
    ("`\n", " "),
    ("`\r\n", " "),
)


def _normalize(text: str) -> str:
    for needle, replacement in _TOKEN_NORMALIZERS:
        text = text.replace(needle, replacement)
    return text.strip()


def _parse_cookie_header(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for segment in raw.split(";"):
        if "=" not in segment:
            continue
        name, value = segment.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            out[name] = value
    return out


def parse_curl(curl_text: str) -> ParsedCurl:
    """Parse a cURL command and return the relevant pieces."""
    if not curl_text or not curl_text.strip():
        return ParsedCurl()

    cleaned = _normalize(curl_text)
    # Some shells prefix the command with ``curl.exe``. Strip both.
    cleaned = re.sub(r"^(curl(\.exe)?)\s+", "", cleaned, flags=re.IGNORECASE)

    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        # Fall back to naive splitting on whitespace.
        tokens = cleaned.split()

    result = ParsedCurl()
    pending_url: str | None = None

    iterator = iter(tokens)
    for token in iterator:
        if not token:
            continue

        if token in {"-H", "--header"}:
            header_value = next(iterator, "")
            if ":" in header_value:
                name, value = header_value.split(":", 1)
                name = name.strip()
                value = value.strip()
                if name.lower() == "cookie":
                    result.cookies.update(_parse_cookie_header(value))
                else:
                    result.headers[name] = value
            continue

        if token in {"-b", "--cookie"}:
            cookie_value = next(iterator, "")
            result.cookies.update(_parse_cookie_header(cookie_value))
            continue

        if token in {"--url"}:
            pending_url = next(iterator, "")
            continue

        if token in {"-X", "--request"}:
            result.method = (next(iterator, "GET") or "GET").upper()
            continue

        if token in {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"}:
            result.body = next(iterator, "")
            if result.method == "GET":
                result.method = "POST"
            continue

        if token.startswith("-"):
            # Unknown flag: consume one argument defensively only when it looks like a value.
            continue

        if token.startswith(("http://", "https://")):
            pending_url = token

    if pending_url:
        parsed = urlparse(pending_url)
        if parsed.scheme and parsed.netloc:
            result.url = pending_url
            if parsed.query:
                result.query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        else:
            result.url = pending_url

    return result
