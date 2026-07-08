"""Tests for fastapi_standalone_di.singleton."""

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from _future_annotations_deps import FutureDb, build_future_db
from fastapi import Depends, FastAPI

from fastapi_standalone_di import (
    AppState,
    FastAPIContainer,
    container_lifespan,
    set_app_state_value,
    singleton,
)

# ``FastAPI(lifespan=...)`` was added in FastAPI 0.93; older floor versions
# silently ignore the argument, so ``container_lifespan`` cannot take effect.
_LIFESPAN_SUPPORTED = "lifespan" in inspect.signature(FastAPI.__init__).parameters


@pytest.fixture(autouse=True)
def _reset_standalone() -> Iterator[None]:
    AppState.reset_standalone()
    yield
    AppState.reset_standalone()


async def _asgi_get(app: FastAPI, path: str) -> Any:
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


@asynccontextmanager
async def _asgi_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the app's ASGI lifespan: startup on enter, shutdown on exit."""
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await to_app.put({"type": "lifespan.startup"})

    async def receive() -> dict[str, Any]:
        return await to_app.get()

    async def send(message: dict[str, Any]) -> None:
        await from_app.put(message)

    task = asyncio.ensure_future(app({"type": "lifespan", "state": {}}, receive, send))
    started = await from_app.get()
    assert started["type"] == "lifespan.startup.complete", started
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        stopped = await from_app.get()
        assert stopped["type"] == "lifespan.shutdown.complete", stopped
        await task


class Settings:
    def __init__(self, url: str = "sqlite://") -> None:
        self.url = url


class Database:
    def __init__(self, url: str) -> None:
        self.url = url


class TestEagerStandalone:
    async def test_builds_once_and_returns_same_instance(self) -> None:
        builds = 0

        @singleton(key="db")
        def get_db() -> Database:
            nonlocal builds
            builds += 1
            return Database("sqlite://")

        async with FastAPIContainer() as container:
            first = await container.get(get_db)
            second = await container.get(get_db)

        assert first is second
        assert builds == 1

    async def test_caches_under_the_given_key_in_app_state(self) -> None:
        @singleton(key="db")
        def get_db() -> Database:
            return Database("sqlite://")

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert AppState.standalone().get("db") is db

    async def test_default_key_is_namespaced_and_isolated(self) -> None:
        @singleton
        def get_db() -> Database:
            return Database("sqlite://")

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert db is not None
        assert AppState.standalone().get("db") is None

    async def test_resolves_factory_sub_dependencies(self) -> None:
        def get_settings() -> Settings:
            return Settings("postgres://host/app")

        @singleton(key="db")
        def get_db(settings: Settings = Depends(get_settings)) -> Database:
            return Database(settings.url)

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert db.url == "postgres://host/app"

    async def test_preset_value_short_circuits_construction(self) -> None:
        builds = 0

        @singleton(key="db")
        def get_db() -> Database:
            nonlocal builds
            builds += 1
            return Database("sqlite://")

        preset = Database("preset://")
        set_app_state_value("db", preset)

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert db is preset
        assert builds == 0

    async def test_async_factory_is_supported(self) -> None:
        @singleton(key="db")
        async def get_db() -> Database:
            return Database("async://")

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert db.url == "async://"

    async def test_concurrent_cold_cache_builds_a_single_instance(self) -> None:
        builds = 0

        @singleton(key="db")
        async def get_db() -> Database:
            nonlocal builds
            builds += 1
            await asyncio.sleep(0.01)
            return Database("sqlite://")

        async with FastAPIContainer() as container:
            results = await asyncio.gather(*(container.get(get_db) for _ in range(10)))

        assert builds == 1
        assert all(r is results[0] for r in results)

    async def test_future_annotations_factory(self) -> None:
        get_db = singleton(build_future_db, key="db")

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert isinstance(db, FutureDb)
        assert db.url == "sqlite://"

    async def test_injected_param_name_avoids_factory_collision(self) -> None:
        @singleton(key="db")
        def get_db(__fsd_app_state__: str = "collision") -> Database:
            return Database(__fsd_app_state__)

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert db.url == "collision"


class TestEagerRejections:
    def test_rejects_sync_generator_factory(self) -> None:
        def get_conn() -> Iterator[Database]:
            yield Database("sqlite://")

        with pytest.raises(TypeError, match="generator dependency"):
            singleton(get_conn)

    def test_rejects_async_generator_factory(self) -> None:
        async def get_conn() -> AsyncIterator[Database]:
            yield Database("sqlite://")

        with pytest.raises(TypeError, match="generator dependency"):
            singleton(get_conn)

    def test_rejects_variadic_factory(self) -> None:
        def get_thing(*args: object, **kwargs: object) -> Database:
            return Database("sqlite://")

        with pytest.raises(TypeError, match="variadic"):
            singleton(get_thing)


class TestEagerASGI:
    async def test_shared_across_requests(self) -> None:
        builds = 0
        settings_resolved = 0

        def get_settings() -> Settings:
            nonlocal settings_resolved
            settings_resolved += 1
            return Settings()

        @singleton(key="db")
        def get_db(settings: Settings = Depends(get_settings)) -> Database:
            nonlocal builds
            builds += 1
            return Database(settings.url)

        app = FastAPI()

        @app.get("/id")
        def route(db: Database = Depends(get_db)) -> dict[str, int]:
            return {"id": id(db)}

        first = (await _asgi_get(app, "/id"))["id"]
        second = (await _asgi_get(app, "/id"))["id"]

        assert first == second
        assert builds == 1
        # The body runs once, but the sub-dependency tree is re-resolved per
        # request (eager mode).
        assert settings_resolved == 2

    async def test_preset_on_app_state_is_used(self) -> None:
        @singleton(key="db")
        def get_db() -> Database:
            raise AssertionError("factory must not run when a value is preset")

        app = FastAPI()
        app.state.db = Database("preset://")

        @app.get("/url")
        def route(db: Database = Depends(get_db)) -> dict[str, str]:
            return {"url": db.url}

        assert (await _asgi_get(app, "/url"))["url"] == "preset://"

    async def test_none_value_is_cached_across_requests(self) -> None:
        builds = 0

        @singleton(key="maybe")
        def get_maybe() -> None:
            nonlocal builds
            builds += 1

        app = FastAPI()

        @app.get("/n")
        def route(value: None = Depends(get_maybe)) -> dict[str, int]:
            return {"builds": builds}

        await _asgi_get(app, "/n")
        await _asgi_get(app, "/n")

        # A legitimate None result is cached, not rebuilt on each request.
        assert builds == 1

    async def test_preset_none_short_circuits(self) -> None:
        @singleton(key="maybe")
        def get_maybe() -> None:
            raise AssertionError("factory must not run when None is preset")

        app = FastAPI()
        app.state.maybe = None

        @app.get("/n")
        def route(value: None = Depends(get_maybe)) -> dict[str, bool]:
            return {"is_none": value is None}

        assert (await _asgi_get(app, "/n"))["is_none"] is True

    async def test_concurrent_requests_build_a_single_instance(self) -> None:
        builds = 0

        @singleton(key="db")
        async def get_db() -> Database:
            nonlocal builds
            builds += 1
            await asyncio.sleep(0.01)
            return Database("sqlite://")

        app = FastAPI()

        @app.get("/id")
        async def route(db: Database = Depends(get_db)) -> dict[str, int]:
            return {"id": id(db)}

        first, second = await asyncio.gather(
            _asgi_get(app, "/id"), _asgi_get(app, "/id")
        )

        assert first["id"] == second["id"]
        assert builds == 1


class TestLazy:
    async def test_fully_lazy_resolves_tree_once(self) -> None:
        settings_resolved = 0
        builds = 0

        def get_settings() -> Settings:
            nonlocal settings_resolved
            settings_resolved += 1
            return Settings("postgres://host/app")

        def build_db(settings: Settings = Depends(get_settings)) -> Database:
            nonlocal builds
            builds += 1
            return Database(settings.url)

        get_db = singleton(build_db, key="db", lazy=True)

        async with FastAPIContainer() as container:
            first = await container.get(get_db)
            second = await container.get(get_db)

        assert first is second
        assert first.url == "postgres://host/app"
        assert builds == 1
        assert settings_resolved == 1

    async def test_yield_teardown_runs_at_container_close(self) -> None:
        events: list[str] = []

        class Conn:
            async def close(self) -> None:
                events.append("close")

        async def get_conn() -> AsyncIterator[Conn]:
            events.append("open")
            conn = Conn()
            try:
                yield conn
            finally:
                await conn.close()

        get_conn_singleton = singleton(get_conn, key="conn", lazy=True)

        async with FastAPIContainer() as container:
            first = await container.get(get_conn_singleton)
            second = await container.get(get_conn_singleton)
            assert first is second
            assert events == ["open"]

        assert events == ["open", "close"]

    async def test_preset_value_short_circuits(self) -> None:
        def build_db() -> Database:
            raise AssertionError("factory must not run when a value is preset")

        get_db = singleton(build_db, key="db", lazy=True)
        preset = Database("preset://")
        set_app_state_value("db", preset)

        async with FastAPIContainer() as container:
            assert await container.get(get_db) is preset

    async def test_preset_none_short_circuits(self) -> None:
        def build_maybe() -> None:
            raise AssertionError("factory must not run when None is preset")

        get_maybe = singleton(build_maybe, key="maybe", lazy=True)
        set_app_state_value("maybe", None)

        async with FastAPIContainer() as container:
            assert await container.get(get_maybe) is None

    async def test_asgi_with_registered_container(self) -> None:
        builds = 0

        def build_db() -> Database:
            nonlocal builds
            builds += 1
            return Database("sqlite://")

        get_db = singleton(build_db, key="db", lazy=True)

        app = FastAPI()

        @app.get("/id")
        def route(db: Database = Depends(get_db)) -> dict[str, int]:
            return {"id": id(db)}

        container = FastAPIContainer(app_state=AppState.from_app(app))
        app.state.container = container

        first = (await _asgi_get(app, "/id"))["id"]
        second = (await _asgi_get(app, "/id"))["id"]

        assert first == second
        assert builds == 1
        await container.aclose()

    async def test_asgi_preset_short_circuits_without_container(self) -> None:
        """A preset value lets a lazy route dependency resolve with no container.

        The wrapper depends on the non-raising container getter, so the preset
        short-circuit is reached even though the ASGI app has no container.
        """

        async def build_conn() -> AsyncIterator[Database]:
            raise AssertionError("factory must not run when a value is preset")
            yield  # pragma: no cover - marks build_conn as a generator factory

        get_db = singleton(build_conn, key="db", lazy=True)

        app = FastAPI()

        @app.get("/url")
        def route(db: Database = Depends(get_db)) -> dict[str, str]:
            return {"url": db.url}

        app.state.db = Database("preset://")

        assert (await _asgi_get(app, "/url"))["url"] == "preset://"

    @pytest.mark.skipif(
        not _LIFESPAN_SUPPORTED, reason="FastAPI(lifespan=...) unsupported < 0.93"
    )
    async def test_asgi_lazy_generator_via_container_lifespan(self) -> None:
        """``container_lifespan`` wires a container so a lazy generator factory
        works as a route dependency, and owns its teardown at app shutdown."""
        events: list[str] = []

        class Conn:
            async def close(self) -> None:
                events.append("close")

        async def get_conn() -> AsyncIterator[Conn]:
            events.append("open")
            conn = Conn()
            try:
                yield conn
            finally:
                await conn.close()

        get_conn_singleton = singleton(get_conn, key="conn", lazy=True)

        app = FastAPI(lifespan=container_lifespan)

        @app.get("/id")
        def route(conn: Conn = Depends(get_conn_singleton)) -> dict[str, int]:
            return {"id": id(conn)}

        async with _asgi_lifespan(app):
            first = (await _asgi_get(app, "/id"))["id"]
            second = (await _asgi_get(app, "/id"))["id"]
            assert first == second
            assert events == ["open"]

        assert events == ["open", "close"]

    async def test_asgi_lazy_without_container_or_preset_raises(self) -> None:
        async def build() -> AsyncIterator[Database]:
            yield Database("x")

        get_db = singleton(build, key="db", lazy=True)

        app = FastAPI()

        @app.get("/url")
        def route(db: Database = Depends(get_db)) -> dict[str, str]:
            return {"url": db.url}

        with pytest.raises(RuntimeError, match="needs a FastAPIContainer"):
            await _asgi_get(app, "/url")


class TestCrossMode:
    async def test_container_build_is_visible_in_asgi_request(self) -> None:
        builds = 0

        @singleton(key="db")
        def get_db() -> Database:
            nonlocal builds
            builds += 1
            return Database("sqlite://")

        app = FastAPI()

        @app.get("/id")
        def route(db: Database = Depends(get_db)) -> dict[str, int]:
            return {"id": id(db)}

        container = FastAPIContainer(app_state=AppState.from_app(app))
        built = await container.get(get_db)

        served = (await _asgi_get(app, "/id"))["id"]

        assert served == id(built)
        assert builds == 1
        await container.aclose()


class TestDecoratorForms:
    async def test_bare_decorator(self) -> None:
        @singleton
        def get_db() -> Database:
            return Database("bare://")

        async with FastAPIContainer() as container:
            assert (await container.get(get_db)).url == "bare://"

    async def test_functional_form(self) -> None:
        def build_db() -> Database:
            return Database("functional://")

        get_db = singleton(build_db, key="db")

        async with FastAPIContainer() as container:
            db = await container.get(get_db)

        assert db.url == "functional://"
        assert AppState.standalone().get("db") is db
