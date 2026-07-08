"""Dependencies declared under PEP 563 (``from __future__ import annotations``).

Their annotations are strings at runtime, which used to defeat the connection
stub injection in the resolver. Kept in a dedicated module so the future import
applies to the whole file.
"""

from __future__ import annotations

from fastapi import Depends

# Must stay a runtime import: FastAPI evaluates these annotations at
# introspection time to detect the connection parameters, even under PEP 563.
from starlette.requests import HTTPConnection, Request  # noqa: TC002


class FutureSettings:
    def __init__(self, url: str = "sqlite://") -> None:
        self.url = url


def get_future_settings() -> FutureSettings:
    return FutureSettings()


class FutureDb:
    def __init__(self, url: str) -> None:
        self.url = url


def build_future_db(
    settings: FutureSettings = Depends(get_future_settings),
) -> FutureDb:
    """A singleton factory whose ``Depends`` annotation is a PEP 563 string."""
    return FutureDb(settings.url)


def needs_request(request: Request) -> str:
    return request.method


def needs_http_connection(conn: HTTPConnection) -> str:
    return conn.scope["type"]


def optional_request(request: Request = None) -> str:  # type: ignore[assignment]
    return "no-request" if request is None else request.method
