"""Standalone supply of query/path/header/cookie parameters.

Outside ASGI these values don't arrive over the wire; the container supplies
them as strings (as HTTP would) and FastAPI coerces each to its declared type.
"""

import pytest
from fastapi import Cookie, Header, Path, Query

from fastapi_standalone_di import (
    FastAPIContainer,
    MissingParameterError,
    ParameterValidationError,
    ParamSource,
)


def _supports_default_factory() -> bool:
    try:
        Query(default_factory=list)
    except TypeError:
        return False
    return True


class TestExplicitValues:
    async def test_values_are_coerced_to_declared_type(self) -> None:
        def handler(
            user_id: int = Path(...), limit: int = Query(...)
        ) -> tuple[int, int]:
            return user_id, limit

        container = FastAPIContainer(path={"user_id": "42"}, query={"limit": "10"})
        assert await container.invoke(handler) == (42, 10)

    async def test_header_and_cookie_sources(self) -> None:
        def handler(
            x_token: str = Header(...), session: str = Cookie(...)
        ) -> tuple[str, str]:
            return x_token, session

        container = FastAPIContainer(
            headers={"x_token": "abc"}, cookies={"session": "xyz"}
        )
        assert await container.invoke(handler) == ("abc", "xyz")

    async def test_dict_shorthand_equals_param_source_values(self) -> None:
        def handler(limit: int = Query(...)) -> int:
            return limit

        from_dict = FastAPIContainer(query={"limit": "5"})
        from_source = FastAPIContainer(query=ParamSource(values={"limit": "5"}))
        assert await from_dict.invoke(handler) == await from_source.invoke(handler) == 5

    async def test_value_matched_by_alias(self) -> None:
        def handler(q: str = Query(..., alias="search")) -> str:
            return q

        container = FastAPIContainer(query={"search": "hello"})
        assert await container.invoke(handler) == "hello"

    async def test_incompatible_value_raises(self) -> None:
        def handler(user_id: int = Path(...)) -> int:
            return user_id

        container = FastAPIContainer(path={"user_id": "notanint"})
        with pytest.raises(ParameterValidationError) as exc:
            await container.invoke(handler)
        assert exc.value.source == "path"
        assert exc.value.name == "user_id"


class TestDeclaredDefaults:
    async def test_declared_default_is_kept(self) -> None:
        def handler(limit: int = Query(10)) -> int:
            return limit

        assert await FastAPIContainer().invoke(handler) == 10

    async def test_source_default_does_not_override_declared_default(self) -> None:
        def handler(limit: int = Query(10)) -> int:
            return limit

        container = FastAPIContainer(query=ParamSource(default="0"))
        assert await container.invoke(handler) == 10

    @pytest.mark.skipif(
        not _supports_default_factory(),
        reason="Query(default_factory=...) unsupported on this FastAPI/pydantic",
    )
    async def test_default_factory_is_called(self) -> None:
        def handler(tags: list[str] = Query(default_factory=list)) -> list[str]:
            return tags

        assert await FastAPIContainer().invoke(handler) == []


class TestSourceDefault:
    async def test_default_fills_required_param(self) -> None:
        def handler(q: str = Query(...)) -> str:
            return q

        container = FastAPIContainer(query=ParamSource(default="fallback"))
        assert await container.invoke(handler) == "fallback"

    async def test_default_is_coerced_per_declared_type(self) -> None:
        def handler(a: int = Query(...), b: str = Query(...)) -> tuple[int, str]:
            return a, b

        container = FastAPIContainer(query=ParamSource(default="0"))
        assert await container.invoke(handler) == (0, "0")

    async def test_default_incompatible_with_type_raises(self) -> None:
        def handler(n: int = Query(...)) -> int:
            return n

        container = FastAPIContainer(query=ParamSource(default=""))
        with pytest.raises(ParameterValidationError):
            await container.invoke(handler)


class TestInferredParams:
    """Bare scalar parameters (no ``Query()``/``Path()`` marker) are classified
    as query parameters by FastAPI, so they resolve through the same path."""

    async def test_inferred_required_without_value_raises(self) -> None:
        def handler(x: int) -> int:
            return x

        with pytest.raises(MissingParameterError) as exc:
            await FastAPIContainer().invoke(handler)
        assert exc.value.source == "query"
        assert exc.value.name == "x"

    async def test_inferred_value_is_coerced(self) -> None:
        def handler(x: int, flag: bool = False) -> tuple[int, bool]:
            return x, flag

        container = FastAPIContainer(query={"x": "42", "flag": "true"})
        assert await container.invoke(handler) == (42, True)

    async def test_inferred_declared_default_is_kept(self) -> None:
        def handler(limit: int = 10) -> int:
            return limit

        assert await FastAPIContainer().invoke(handler) == 10

    async def test_inferred_filled_by_source_default(self) -> None:
        def handler(x: int) -> int:
            return x

        container = FastAPIContainer(query=ParamSource(default="0"))
        assert await container.invoke(handler) == 0


class TestMissingRequired:
    async def test_required_without_value_raises(self) -> None:
        def handler(q: str = Query(...)) -> str:
            return q

        with pytest.raises(MissingParameterError) as exc:
            await FastAPIContainer().invoke(handler)
        assert exc.value.source == "query"
        assert exc.value.name == "q"

    async def test_error_names_param_and_source(self) -> None:
        def handler(x_token: str = Header(...)) -> str:
            return x_token

        with pytest.raises(MissingParameterError, match="header parameter 'x_token'"):
            await FastAPIContainer().invoke(handler)
