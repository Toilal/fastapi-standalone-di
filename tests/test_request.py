"""The stub Request injected for connection parameters outside ASGI.

One Request is built per resolution operation, shared across that operation's
dependency tree and isolated between operations; its scope is complete enough
that app/state/body/query/path/cookies all work.
"""

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, Request

from fastapi_standalone_di import (
    AppState,
    FastAPIContainer,
    set_app_state_value,
)


@pytest.fixture(autouse=True)
def _reset_standalone() -> Iterator[None]:
    AppState.reset_standalone()
    yield
    AppState.reset_standalone()


class TestCompleteScope:
    async def test_body_returns_empty(self) -> None:
        async def handler(request: Request) -> bytes:
            return await request.body()

        assert await FastAPIContainer().invoke(handler) == b""

    async def test_connection_attributes_present(self) -> None:
        def handler(request: Request) -> tuple[object, object, str]:
            return request.client, request.scope["server"], request.url.scheme

        client, server, scheme = await FastAPIContainer().invoke(handler)
        # No client outside ASGI. Older Starlette wraps it as Address(None, None)
        # rather than returning None, so assert on the absence of a host.
        assert client is None or client.host is None
        assert server == ("standalone", 0)
        assert scheme == "http"


class TestAppState:
    async def test_app_state_mirrors_container(self) -> None:
        def handler(request: Request) -> str:
            return request.app.state.db

        set_app_state_value("db", "my-db")
        assert await FastAPIContainer().invoke(handler) == "my-db"

    async def test_real_app_is_passed_through(self) -> None:
        app = FastAPI()

        def handler(request: Request) -> bool:
            return request.app is app

        container = FastAPIContainer(app=app, app_state=AppState.from_app(app))
        assert await container.invoke(handler) is True


class TestConfigReflection:
    async def test_query_path_cookies_reflect_config(self) -> None:
        def handler(request: Request) -> dict[str, object]:
            return {
                "query": dict(request.query_params),
                "path": request.path_params,
                "cookies": request.cookies,
            }

        container = FastAPIContainer(
            query={"limit": "10"},
            path={"user_id": "42"},
            cookies={"session": "xyz"},
        )
        assert await container.invoke(handler) == {
            "query": {"limit": "10"},
            "path": {"user_id": "42"},
            "cookies": {"session": "xyz"},
        }

    async def test_cookie_special_chars_round_trip(self) -> None:
        def handler(request: Request) -> dict[str, str]:
            return dict(request.cookies)

        cookies = {"sid": "a; b=c", "plain": "hello world"}
        container = FastAPIContainer(cookies=cookies)
        assert await container.invoke(handler) == cookies

    async def test_non_latin1_cookie_value_raises(self) -> None:
        def handler(request: Request) -> dict[str, str]:
            return dict(request.cookies)

        container = FastAPIContainer(cookies={"sid": "☕"})
        with pytest.raises(UnicodeEncodeError):
            await container.invoke(handler)


class TestSharingAndIsolation:
    async def test_state_shared_within_resolution_tree(self) -> None:
        def writer(request: Request) -> str:
            request.state.trace = "seen"
            return "ok"

        def reader(request: Request, _: str = Depends(writer)) -> str | None:
            return getattr(request.state, "trace", None)

        assert await FastAPIContainer().invoke(reader) == "seen"

    async def test_state_isolated_between_operations(self) -> None:
        container = FastAPIContainer()

        def writer(request: Request) -> None:
            request.state.trace = "leak"

        def probe(request: Request) -> str:
            return getattr(request.state, "trace", "clean")

        await container.invoke(writer)
        assert await container.invoke(probe) == "clean"

    async def test_each_operation_gets_a_distinct_request(self) -> None:
        container = FastAPIContainer()
        seen: list[Request] = []

        def capture(request: Request) -> None:
            seen.append(request)

        await container.invoke(capture)
        await container.invoke(capture)
        assert seen[0] is not seen[1]


class TestLazyBuild:
    """The stub Request is built lazily: only when a dependency needs one."""

    @staticmethod
    def _count_builds(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
        import fastapi_standalone_di.resolve as resolve_mod

        builds = [0]
        real = resolve_mod._build_stub_request

        def counting(**kwargs: object) -> Request:
            builds[0] += 1
            return real(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(resolve_mod, "_build_stub_request", counting)
        return builds

    async def test_no_connection_param_builds_no_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builds = self._count_builds(monkeypatch)

        def leaf() -> str:
            return "leaf"

        def root(value: str = Depends(leaf)) -> str:
            return value

        assert await FastAPIContainer().invoke(root) == "leaf"
        assert builds[0] == 0

    async def test_connection_param_builds_one_shared_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builds = self._count_builds(monkeypatch)
        seen: list[Request] = []

        def writer(request: Request) -> None:
            seen.append(request)

        def reader(request: Request, _: None = Depends(writer)) -> None:
            seen.append(request)

        await FastAPIContainer().invoke(reader)
        assert builds[0] == 1
        assert len(seen) == 2
        assert seen[0] is seen[1]
