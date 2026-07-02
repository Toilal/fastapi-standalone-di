"""Application state abstraction for FastAPI and standalone contexts.

``AppState`` provides a unified interface to access application-level state
(database clients, caches, managers, …) regardless of whether the code runs
inside a FastAPI ASGI request or in a standalone context (CLI scripts,
background tasks, :class:`~fastapi_standalone_di.resolve.FastAPIContainer`).

In **FastAPI mode**, ``AppState`` delegates to ``request.app.state`` (Starlette's
built-in state). In **standalone mode**, it falls back to an internal dict.

Usage in a FastAPI dependency::

    def get_db(app_state: AppState = Depends(get_app_state)) -> Database:
        return app_state.get("db")

Usage with ``FastAPIContainer``::

    set_app_state_value("db", db_client)
    container = FastAPIContainer()
    db = await container.get(get_db)
"""

from typing import Any, ClassVar, Self

from fastapi import FastAPI
from starlette.datastructures import State
from starlette.requests import HTTPConnection


class AppState:
    """Abstraction over Starlette's ``State`` that works with and without a request.

    *  **FastAPI mode** — created via :meth:`from_request`; reads/writes go to
       ``request.app.state``.
    *  **Standalone mode** — created via :meth:`standalone`; reads/writes go to
       a module-level singleton dict.
    """

    _standalone_instance: ClassVar[Self | None] = None

    def __init__(self, state: State | None = None) -> None:
        self._state = state
        self._store: dict[str, Any] = {}

    # --- read / write ---------------------------------------------------------

    def get(self, key: str) -> Any | None:
        if self._state is not None:
            return getattr(self._state, key, None)
        return self._store.get(key)

    def set(self, key: str, value: Any) -> None:
        if self._state is not None:
            setattr(self._state, key, value)
            # Keep the standalone singleton in sync so that FastAPIContainer
            # and other non-request code can resolve the same values.
            standalone = type(self).standalone()
            if standalone is not self:
                standalone._store[key] = value
        self._store[key] = value

    def delete(self, key: str) -> None:
        if self._state is not None and hasattr(self._state, key):
            delattr(self._state, key)
            standalone = type(self).standalone()
            if standalone is not self:
                standalone._store.pop(key, None)
        self._store.pop(key, None)

    # --- constructors ---------------------------------------------------------

    @classmethod
    def from_request(cls, request: HTTPConnection) -> Self:
        """Create an ``AppState`` backed by the ASGI application state."""
        return cls(state=request.app.state)

    @classmethod
    def from_app(cls, app: FastAPI) -> Self:
        """Create an ``AppState`` backed by a Starlette/FastAPI application."""
        return cls(state=app.state)

    @classmethod
    def standalone(cls) -> Self:
        """Return the module-level singleton (no ASGI context needed)."""
        if cls._standalone_instance is None:
            cls._standalone_instance = cls()
        return cls._standalone_instance

    @classmethod
    def reset_standalone(cls) -> None:
        """Reset the standalone singleton (useful in tests)."""
        cls._standalone_instance = None


# --- FastAPI dependency -------------------------------------------------------


def get_app_state(
    request: HTTPConnection = None,  # type: ignore[assignment]
) -> AppState:
    """FastAPI dependency that returns an :class:`AppState`.

    When injected by FastAPI, *request* is provided automatically and the
    returned ``AppState`` delegates to ``request.app.state``. When resolved
    outside ASGI (e.g. via :class:`FastAPIContainer`), *request* is ``None`` and
    the standalone singleton is used instead.
    """
    if request is not None:
        return AppState.from_request(request)
    return AppState.standalone()


# --- convenience helpers for startup / scripts --------------------------------


def set_app_state_value(key: str, value: Any) -> None:
    """Set a value in the standalone ``AppState`` store.

    Call this at application startup (alongside ``app.state.xxx = …``) so that
    the value is available for both FastAPI and standalone contexts.
    """
    AppState.standalone().set(key, value)
