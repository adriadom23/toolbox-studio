from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.security import new_token

SESSION_COOKIE = "studio_session"
CSRF_COOKIE = "studio_csrf"


@dataclass(frozen=True)
class Session:
    identity: str
    csrf_token: str
    expires_at: float


class SessionStore:
    def __init__(self, ttl_seconds: int = 8 * 60 * 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, Session] = {}

    def create(self, identity: str) -> tuple[str, Session]:
        session_id = new_token()
        session = Session(identity=identity, csrf_token=new_token(), expires_at=time.time() + self.ttl_seconds)
        self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str | None) -> Session | None:
        session = self._sessions.get(session_id or "")
        if session is None or session.expires_at < time.time():
            return None
        return session
