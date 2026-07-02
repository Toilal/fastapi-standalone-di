"""Dependencies declared under PEP 563 (``from __future__ import annotations``).

Their annotations are strings at runtime, which used to defeat the connection
stub injection in the resolver. Kept in a dedicated module so the future import
applies to the whole file.
"""

from __future__ import annotations

# Must stay a runtime import: FastAPI evaluates these annotations at
# introspection time to detect the connection parameters, even under PEP 563.
from starlette.requests import HTTPConnection, Request  # noqa: TC002


def needs_request(request: Request) -> str:
    return request.method


def needs_http_connection(conn: HTTPConnection) -> str:
    return conn.scope["type"]


def optional_request(request: Request = None) -> str:  # type: ignore[assignment]
    return "no-request" if request is None else request.method
