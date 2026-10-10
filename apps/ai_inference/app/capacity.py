"""Bound request bytes before JSON parsing, including chunked transport."""

import asyncio
from uuid import uuid4

from fastapi.responses import JSONResponse


class RequestCapacity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        size, events = 0, []
        try:
            async with asyncio.timeout(10):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    size += len(event.get("body", b""))
                    if size > 64 * 1024:
                        raise ValueError
                    events.append(event)
                    if not event.get("more_body", False):
                        break
        except (TimeoutError, ValueError):
            value = {
                "request_id": str(uuid4()),
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Inference request exceeds transport capacity",
                    "retryable": False,
                    "details": [],
                },
            }
            return await JSONResponse(value, status_code=422)(scope, receive, send)

        async def buffered():
            return events.pop(0) if events else await receive()

        await self.app(scope, buffered, send)
