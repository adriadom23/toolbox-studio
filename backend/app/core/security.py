from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Mapping
from datetime import datetime, timezone

_SECRET_KEY = re.compile(r"(authorization|cookie|secret|token|password|webhook|signature)", re.I)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def redact(value):
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def proxy_signature(
    secret: str,
    identity: str,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    *,
    query: str = "",
    body_sha256: str | None = None,
    roles: str = "",
    request_id: str = "",
) -> str:
    body_digest = body_sha256 if body_sha256 is not None else hashlib.sha256(b"").hexdigest()
    payload = "\n".join((method.upper(), path, query, body_digest, identity, roles, timestamp, nonce, request_id)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class ReplayGuard:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def consume(self, nonce: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        self._seen = {key: seen for key, seen in self._seen.items() if current - seen <= self.ttl_seconds}
        if not nonce or nonce in self._seen:
            return False
        self._seen[nonce] = current
        return True


def consume_proxy_nonce(connection, *, nonce: str, actor: str, request_id: str, timestamp: float, ttl_seconds: int = 300) -> bool:
    if not nonce or not request_id:
        return False
    nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    expires_at = datetime.fromtimestamp(timestamp + ttl_seconds, timezone.utc).isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM proxy_replay_nonces WHERE expires_at < ?", (created_at,))
        connection.execute(
            "INSERT INTO proxy_replay_nonces(nonce_hash,actor_id,request_id,expires_at,created_at) VALUES (?,?,?,?,?)",
            (nonce_hash, actor, request_id, expires_at, created_at),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        return False
    return True


def verify_proxy_request(
    *,
    secret: str,
    identity: str,
    timestamp: str,
    nonce: str,
    signature: str,
    method: str,
    path: str,
    replay_guard: ReplayGuard | None = None,
    now: float | None = None,
    query: str = "",
    body_sha256: str | None = None,
    roles: str = "",
    request_id: str = "",
    nonce_consumer=None,
) -> bool:
    current = time.time() if now is None else now
    try:
        issued = float(timestamp)
    except (TypeError, ValueError):
        return False
    ttl = replay_guard.ttl_seconds if replay_guard else 300
    if abs(current - issued) > ttl:
        return False
    expected = proxy_signature(
        secret, identity, timestamp, nonce, method, path,
        query=query, body_sha256=body_sha256, roles=roles, request_id=request_id,
    )
    if not hmac.compare_digest(expected, signature or ""):
        return False
    if nonce_consumer is not None:
        return bool(nonce_consumer(nonce, identity, request_id, issued, ttl))
    return bool(replay_guard and replay_guard.consume(nonce, current))


def encode_cursor(secret: str, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")


def decode_cursor(secret: str, value: str) -> dict[str, object]:
    try:
        packed = base64.urlsafe_b64decode(value + "===")
        raw, signature = packed[:-32], packed[-32:]
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        if len(signature) != 32 or not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def verify_csrf(cookie_token: str | None, header_token: str | None) -> bool:
    return bool(cookie_token and header_token and hmac.compare_digest(cookie_token, header_token))
