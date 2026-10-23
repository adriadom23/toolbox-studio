from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.errors import PublicError

_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


@dataclass(frozen=True)
class IdempotencyResult:
    kind: str
    operation_id: str
    status_code: int | None = None
    response: dict[str, object] | None = None


def canonical_request_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_idempotency_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key or ""):
        raise PublicError("invalid_idempotency_key", "Idempotency-Key must contain 8-128 safe characters", 422)
    return key


def reserve(connection, *, scope: str, actor_id: str, route_template: str, resource_id: str, key: str, request_sha256: str, operation_id: str) -> IdempotencyResult:
    validate_idempotency_key(key)
    row = connection.execute("SELECT * FROM idempotency_records WHERE scope=? AND actor_id=? AND route_template=? AND resource_id=? AND key=?", (scope, actor_id, route_template, resource_id, key)).fetchone()
    if row:
        if row["request_sha256"] != request_sha256:
            raise PublicError("idempotency_mismatch", "Idempotency-Key was used for another request", 409)
        response = json.loads(row["response_json"]) if row["response_json"] else None
        return IdempotencyResult(row["status"], row["operation_id"], row["status_code"], response)
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    connection.execute("INSERT INTO idempotency_records(scope,actor_id,route_template,resource_id,key,request_sha256,status,operation_id,expires_at) VALUES (?,?,?,?,?,?,'pending',?,?)", (scope, actor_id, route_template, resource_id, key, request_sha256, operation_id, expires))
    return IdempotencyResult("new", operation_id)


def complete(connection, operation_id: str, status_code: int, response: dict[str, object]) -> None:
    encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    connection.execute("UPDATE idempotency_records SET status='completed', status_code=?, response_json=? WHERE operation_id=?", (status_code, encoded, operation_id))
