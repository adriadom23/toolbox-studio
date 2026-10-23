from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PublicError(Exception):
    code: str
    detail: str
    status_code: int = 400
    recoverable: bool = False
    field: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self, correlation_id: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "error": self.code,
            "message": self.detail,
            "recoverable": self.recoverable,
            "correlation_id": correlation_id or "unknown",
        }
        if self.field:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload


class ConflictError(PublicError):
    def __init__(self, detail: str = "resource changed") -> None:
        super().__init__("conflict", detail, 409)


class AuthorizationError(PublicError):
    def __init__(self, detail: str = "authorization required") -> None:
        super().__init__("unauthorized", detail, 401)
