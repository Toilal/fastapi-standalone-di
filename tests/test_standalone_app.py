"""End-to-end: a realistic standalone application wired by FastAPIContainer.

No ASGI server — a plain async "worker" resolves a real service graph
(settings -> DB connection (yield) -> repository -> service) and runs it,
checking that the yield dependency's teardown fires when the container closes.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import Depends, Query, Request

from fastapi_standalone_di import (
    AppState,
    DependantCache,
    FastAPIContainer,
    RegistrableDependency,
    get_app_state,
    get_container,
    set_app_state_value,
)

# --- application code ------------------------------------------------------


class Settings:
    def __init__(self, greeting: str = "Hello") -> None:
        self.greeting = greeting


def get_settings(app_state: AppState = Depends(get_app_state)) -> Settings:
    return app_state.get("settings") or Settings()


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def query_names(self) -> list[str]:
        if self.closed:
            raise RuntimeError("connection used after close")
        return ["Ada", "Alan"]


_events: list[str] = []


async def get_connection() -> AsyncIterator[FakeConnection]:
    conn = FakeConnection()
    _events.append("open")
    try:
        yield conn
    finally:
        conn.closed = True
        _events.append("close")


class IUserRepository(ABC, RegistrableDependency):
    @abstractmethod
    def names(self) -> list[str]: ...


class UserRepository(IUserRepository):
    def __init__(self, conn: FakeConnection = Depends(get_connection)) -> None:
        self.conn = conn

    def names(self) -> list[str]:
        return self.conn.query_names()


class IGreeter(ABC, RegistrableDependency):
    @abstractmethod
    def greet_all(self) -> list[str]: ...


class Greeter(IGreeter):
    def __init__(
        self,
        repo: IUserRepository = Depends(IUserRepository),
        settings: Settings = Depends(get_settings),
    ) -> None:
        self.repo = repo
        self.settings = settings

    def greet_all(self) -> list[str]:
        return [f"{self.settings.greeting}, {name}!" for name in self.repo.names()]


# --- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_env() -> Iterator[None]:
    AppState.reset_standalone()
    _events.clear()
    IUserRepository.register(UserRepository)
    IGreeter.register(Greeter)
    yield
    IUserRepository.register(None)
    IGreeter.register(None)
    AppState.reset_standalone()


# --- tests -----------------------------------------------------------------


class TestStandaloneApplication:
    async def test_resolve_and_run_the_service_graph(self) -> None:
        set_app_state_value("settings", Settings(greeting="Hi"))

        async with FastAPIContainer() as container:
            greeter = await container.get(IGreeter)
            assert greeter.greet_all() == ["Hi, Ada!", "Hi, Alan!"]
            # the yield-based connection is open while the container is alive
            assert _events == ["open"]

        # closing the container runs the yield dependency's teardown
        assert _events == ["open", "close"]

    async def test_default_settings_when_state_unset(self) -> None:
        async with FastAPIContainer() as container:
            greeter = await container.get(IGreeter)
            assert greeter.greet_all()[0] == "Hello, Ada!"

    async def test_transitive_singletons_share_one_connection(self) -> None:
        async with FastAPIContainer() as container:
            repo1 = await container.get(IUserRepository)
            repo2 = await container.get(IUserRepository)
            greeter = await container.get(IGreeter)
            assert repo1 is repo2  # class deps are cached on the container
            assert greeter.repo.conn is repo1.conn  # same yielded connection

    async def test_teardown_runs_even_on_error(self) -> None:
        async def run_and_fail() -> None:
            async with FastAPIContainer() as container:
                await container.get(IUserRepository)
                assert _events == ["open"]
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await run_and_fail()
        assert _events == ["open", "close"]

    async def test_connection_usable_before_close_unusable_after(self) -> None:
        container = FastAPIContainer()
        repo = await container.get(IUserRepository)
        assert repo.names() == ["Ada", "Alan"]
        await container.aclose()
        assert repo.conn.closed is True

    async def test_entry_point_invoke_not_cached(self) -> None:
        calls = 0

        async def main(greeter: IGreeter = Depends(IGreeter)) -> int:
            nonlocal calls
            calls += 1
            return len(greeter.greet_all())

        async with FastAPIContainer() as container:
            assert await container.invoke(main) == 2
            await container.invoke(main)
            # invoke() treats the callable as an entry point: not cached
            assert calls == 2


class TestRequestAndParamFallbacks:
    async def test_request_param_gets_a_stub_outside_asgi(self) -> None:
        def needs_request(request: Request) -> str:
            return request.method

        async with FastAPIContainer() as container:
            assert await container.get(needs_request) == "GET"

    async def test_query_param_uses_its_declared_default(self) -> None:
        def paginate(limit: int = Query(50)) -> int:
            return limit

        async with FastAPIContainer() as container:
            assert await container.get(paginate) == 50


class TestGetContainerDependency:
    def test_raises_when_no_container_registered(self) -> None:
        with pytest.raises(RuntimeError, match="No FastAPIContainer registered"):
            get_container(AppState.standalone())

    def test_returns_the_registered_container(self) -> None:
        container = FastAPIContainer()
        set_app_state_value("container", container)
        assert get_container(AppState.standalone()) is container


class TestDependantCacheClear:
    async def test_clear_empties_the_cache(self) -> None:
        cache = DependantCache()
        container = FastAPIContainer(dependant_cache=cache)
        await container.get(IGreeter)
        assert cache.dependants
        cache.clear()
        assert not cache.dependants
