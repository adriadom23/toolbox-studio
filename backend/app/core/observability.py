from __future__ import annotations

import logging
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("toolbox_studio")


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        correlation_id = headers.get(b"x-correlation-id", b"").decode("ascii", "ignore")[:128] or str(uuid.uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-correlation-id", correlation_id.encode("ascii")))
            await send(message)

        await self.app(scope, receive, send_with_id)
