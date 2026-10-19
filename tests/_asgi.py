"""Raw ASGI request helper shared by the tests that go through FastAPI."""

import json
from typing import Any

from fastapi import FastAPI


async def asgi_get(app: FastAPI, path: str) -> Any:
    """Drive the ASGI app for one GET and return its decoded JSON body.

    Speaks the ASGI protocol directly rather than via Starlette's ``TestClient``,
    which pulls in ``requests`` on older Starlette releases and so is unavailable
    across the whole supported FastAPI range.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    body = bytearray()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return json.loads(bytes(body))
