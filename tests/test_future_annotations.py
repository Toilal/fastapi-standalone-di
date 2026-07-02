"""Resolving dependencies declared under PEP 563 (future annotations)."""

from _future_annotations_deps import (
    needs_http_connection,
    needs_request,
    optional_request,
)

from fastapi_standalone_di import FastAPIContainer


class TestFutureAnnotations:
    async def test_request_param_injected_under_pep563(self) -> None:
        c = FastAPIContainer()
        assert await c.get(needs_request) == "GET"

    async def test_http_connection_param_injected_under_pep563(self) -> None:
        c = FastAPIContainer()
        assert await c.get(needs_http_connection) == "http"

    async def test_optional_request_left_to_default(self) -> None:
        c = FastAPIContainer()
        assert await c.get(optional_request) == "no-request"
