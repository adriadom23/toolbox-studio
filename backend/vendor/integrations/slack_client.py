from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

import requests


SLACK_MAX_CHARS = 3900


def _split_slack_message(text: str, limit: int = SLACK_MAX_CHARS) -> list[str]:
    """Split a Slack message into chunks that respect line boundaries.

    Tries to split at blank-line boundaries first (paragraph breaks),
    falling back to line breaks. Code blocks (``` ... ```) that get split
    across chunks are rebalanced so each chunk carries its own opening and
    closing fences (otherwise Slack renders the table cut off / unformatted).
    """
    if not text or len(text) <= limit:
        return [text]

    # Reserve a little headroom so the fence rebalancing (up to 8 chars per
    # chunk) can never push a chunk back over the real Slack limit.
    split_limit = max(1, limit - 8)

    # Try splitting by double-newline (paragraph) boundaries first
    blocks = [b.rstrip() for b in text.split("\n\n") if b.strip()]
    if len(blocks) > 1:
        chunks: list[str] = []
        current = ""
        for block in blocks:
            if len(block) > split_limit:
                # Block itself is too large — split line by line
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(_split_by_lines(block, split_limit))
                continue
            candidate = f"{current}\n\n{block}" if current else block
            if len(candidate) <= split_limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = block
        if current:
            chunks.append(current)
        chunks = chunks or [text]
    else:
        # Single block — split by lines
        chunks = _split_by_lines(text, split_limit)

    return _rebalance_code_fences(chunks)


def _rebalance_code_fences(chunks: list[str]) -> list[str]:
    """Ensure every chunk has balanced ``` code fences.

    When a code block is split across chunks, the opening fence lands in one
    chunk and the closing fence in another, leaving Slack unable to render the
    table. This reopens the block at the start of each continuation chunk and
    closes any block left open at the end of a chunk.
    """
    if len(chunks) <= 1:
        return chunks

    result: list[str] = []
    carry_open = False  # inside an unclosed code block carried from prev chunk
    for chunk in chunks:
        prefix = "```\n" if carry_open else ""
        ends_open = carry_open ^ (chunk.count("```") % 2 == 1)
        suffix = "\n```" if ends_open else ""
        result.append(f"{prefix}{chunk}{suffix}")
        carry_open = ends_open

    return result


def _split_by_lines(text: str, limit: int) -> list[str]:
    """Split text by line boundaries respecting the character limit."""
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        # If a single line exceeds the limit, hard-cut it
        if line_len > limit and not current:
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
        current.append(line)
        current_len += len(line) + 1

    if current:
        chunks.append("\n".join(current))

    return chunks if chunks else [text]


def post_message(webhook_url: str, text: str, timeout: int = 20) -> dict[str, Any]:
    """Post a message to a Slack webhook, automatically splitting if over 3900 chars."""
    try:
        from app.core.locale import locale_from_request
        from app.integrations.messages_i18n import translate_message

        text = translate_message(text, locale_from_request())
    except Exception:  # noqa: BLE001
        pass
    chunks = _split_slack_message(text)
    last_response: dict[str, Any] = {}

    for chunk in chunks:
        response = requests.post(webhook_url, json={"text": chunk}, timeout=timeout)
        last_response = {"status_code": response.status_code, "ok": response.ok, "text": response.text}
        if not response.ok:
            return last_response

    return last_response


def send_slack_audit(process_name: str, station_code: Optional[str] = None) -> bool:
    """Post an audit line to the shared Slack audit channel.

    Ported from the legacy ToolBox. Fires a one-line notification of the
    form ``"{STATION} - {process} - {dd/mm/YYYY} - {HH:MM:SS}"`` to the
    webhook configured in ``SLACK_AUDIT_WEBHOOK``. Best-effort: any failure
    (missing webhook, empty process name, network error) is swallowed and
    reported via the boolean return so it can never break a tool run.
    """
    try:
        # Prefer the env_loader cleaner (handles quoted / r"" values) but
        # degrade gracefully if it can't be imported.
        try:
            from app.core.env_loader import clean_env_value

            webhook = clean_env_value(os.getenv("SLACK_AUDIT_WEBHOOK", ""))
        except Exception:  # noqa: BLE001
            webhook = (os.getenv("SLACK_AUDIT_WEBHOOK", "") or "").strip()

        if not webhook or not process_name:
            return False

        # Respect the navbar station selection when one is active, then
        # fall back to the explicit arg and finally the configured env.
        # The "__ALL__" sentinel (navbar "Todos" on multi-site nodes) is
        # never a station: drop it so the fallback resolves the mother
        # node (the configured primary STATION_CODE).
        station = (station_code or "").strip()
        if station.upper() == "__ALL__":
            station = ""
        if not station:
            try:
                from app.core.station_context import resolve_station_code

                station = resolve_station_code(
                    default=(os.getenv("STATION_CODE", "") or "").strip()
                )
            except Exception:  # noqa: BLE001
                station = (os.getenv("STATION_CODE", "") or "").strip()
        station = station.upper() or "UNKNOWN"

        now = datetime.now()
        text = f"{station} - {process_name} - {now.strftime('%d/%m/%Y')} - {now.strftime('%H:%M:%S')}"

        response = requests.post(webhook, json={"text": text}, timeout=10)
        return response.ok
    except BaseException:  # noqa: BLE001
        return False


# =============================================================================
# Optional inbound (read) side — used by the Daily Summary tool.
#
# The rest of this module is outbound-only (incoming webhooks). Reading
# messages needs a Slack app token with the ``channels:history`` /
# ``groups:history`` scope, which most stations do NOT have configured.
# This block is therefore fully optional and degrades to "[]" (with
# ``read_enabled()`` returning False) when the token/channels are absent,
# so the summary still works from Outlook alone.
#
# Configure via env (workbook / .env):
#   * SLACK_API_TOKEN        xoxb-... or xoxp-... bot/user token
#   * SLACK_SUMMARY_CHANNELS comma-separated channel IDs (e.g. C0123,C0456)
# =============================================================================

SLACK_HISTORY_URL = "https://slack.com/api/conversations.history"


def _clean_env(key: str) -> str:
    try:
        from app.core.env_loader import clean_env_value

        return clean_env_value(os.getenv(key, ""))
    except Exception:  # noqa: BLE001
        return (os.getenv(key, "") or "").strip()


def _read_token() -> str:
    return _clean_env("SLACK_API_TOKEN")


def _summary_channels() -> list[str]:
    raw = _clean_env("SLACK_SUMMARY_CHANNELS")
    return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]


def read_enabled() -> bool:
    """True when a Slack API token AND at least one channel are configured."""
    return bool(_read_token()) and bool(_summary_channels())


def get_messages(hours: int = 24, max_per_channel: int = 60, timeout: int = 20) -> list[dict[str, Any]]:
    """Read recent messages from the configured Slack channels.

    Returns a list of ``{channel, user, text, time}`` dicts. Best-effort:
    returns ``[]`` when reading is not configured or any API call fails, so
    the Daily Summary tool never breaks on the Slack side.
    """
    token = _read_token()
    channels = _summary_channels()
    if not token or not channels:
        return []

    from datetime import datetime as _dt, timedelta as _td

    oldest = (_dt.now() - _td(hours=max(1, hours))).timestamp()
    headers = {"Authorization": f"Bearer {token}"}
    messages: list[dict[str, Any]] = []

    for channel in channels:
        try:
            response = requests.get(
                SLACK_HISTORY_URL,
                headers=headers,
                params={"channel": channel, "oldest": f"{oldest:.6f}", "limit": max_per_channel},
                timeout=timeout,
            )
            data = response.json() if response.ok else {}
        except Exception:  # noqa: BLE001
            continue
        if not data.get("ok"):
            continue
        for msg in data.get("messages", []):
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            try:
                ts = float(msg.get("ts", "0"))
                stamp = _dt.fromtimestamp(ts).strftime("%d/%m %H:%M")
            except Exception:  # noqa: BLE001
                stamp = ""
            messages.append(
                {
                    "channel": channel,
                    "user": str(msg.get("user") or msg.get("username") or "?"),
                    "text": text,
                    "time": stamp,
                }
            )

    return messages
