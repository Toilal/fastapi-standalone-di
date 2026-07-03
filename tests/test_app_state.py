"""Tests for fastapi_standalone_di.app_state."""

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from starlette.requests import Request

from fastapi_standalone_di import (
    AppState,
    FastAPIContainer,
    get_app_state,
    set_app_state_value,
)


@pytest.fixture(autouse=True)
def _reset_standalone() -> Iterator[None]:
    AppState.reset_standalone()
    yield
    AppState.reset_standalone()


def _make_request(app: FastAPI) -> Request:
    return Request(
        scope={
            "type": "http",
            "method": "GET",
            "headers": [],
            "query_string": b"",
            "path": "/",
            "app": app,
        }
    )


class TestStandaloneMode:
    def test_standalone_is_singleton(self) -> None:
        assert AppState.standalone() is AppState.standalone()

    def test_reset_standalone_creates_new_instance(self) -> None:
        first = AppState.standalone()
        AppState.reset_standalone()
        assert AppState.standalone() is not first

    def test_get_missing_key_returns_none(self) -> None:
        assert AppState.standalone().get("missing") is None

    def test_set_and_get(self) -> None:
        state = AppState.standalone()
        state.set("db", "value")
        assert state.get("db") == "value"

    def test_delete(self) -> None:
        state = AppState.standalone()
        state.set("db", "value")
        state.delete("db")
        assert state.get("db") is None

    def test_delete_missing_is_noop(self) -> None:
        AppState.standalone().delete("missing")  # must not raise

    def test_set_app_state_value_helper(self) -> None:
        set_app_state_value("cache", 42)
        assert AppState.standalone().get("cache") == 42


class TestRequestMode:
    def test_from_request_reads_app_state(self) -> None:
        app = FastAPI()
        app.state.db = "from-app"
        request = _make_request(app)

        state = AppState.from_request(request)
        assert state.get("db") == "from-app"

    def test_from_app_reads_app_state(self) -> None:
        app = FastAPI()
        app.state.db = "from-app"

        state = AppState.from_app(app)
        assert state.get("db") == "from-app"

    def test_set_writes_to_app_state_only(self) -> None:
        app = FastAPI()
        state = AppState.from_app(app)
        state.set("db", "value")

        assert app.state.db == "value"

    def test_set_does_not_leak_to_standalone(self) -> None:
        app = FastAPI()
        AppState.from_app(app).set("db", "value")

        assert AppState.standalone().get("db") is None

    def test_delete_removes_from_app_state(self) -> None:
        app = FastAPI()
        state = AppState.from_app(app)
        state.set("db", "value")
        state.delete("db")

        assert state.get("db") is None

    def test_delete_does_not_touch_standalone(self) -> None:
        set_app_state_value("db", "global")
        app = FastAPI()
        state = AppState.from_app(app)
        state.set("db", "request")
        state.delete("db")

        assert AppState.standalone().get("db") == "global"

    def test_two_apps_are_isolated(self) -> None:
        app_a, app_b = FastAPI(), FastAPI()
        AppState.from_app(app_a).set("db", "a")
        AppState.from_app(app_b).set("db", "b")

        assert AppState.from_app(app_a).get("db") == "a"
        assert AppState.from_app(app_b).get("db") == "b"


class TestGetAppStateDependency:
    def test_without_request_returns_standalone(self) -> None:
        assert get_app_state() is AppState.standalone()

    def test_with_request_returns_request_backed(self) -> None:
        app = FastAPI()
        app.state.db = "x"
        state = get_app_state(_make_request(app))
        assert state.get("db") == "x"

    async def test_resolved_through_container(self) -> None:
        set_app_state_value("db", "resolved")

        async def get_db(app_state: AppState = Depends(get_app_state)) -> str:
            return app_state.get("db")

        container = FastAPIContainer()
        assert await container.get(get_db) == "resolved"
