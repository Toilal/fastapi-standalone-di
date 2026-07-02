# fastapi-standalone-di

Use [FastAPI](https://fastapi.tiangolo.com/)'s dependency injection **outside of any web/ASGI context**.

FastAPI ships a powerful dependency injection system — `Depends`, sub-dependencies,
`yield` teardown, per-resolution caching. It is, however, tightly coupled to the
request/response cycle. `fastapi-standalone-di` reuses that exact machinery
(`get_dependant`, the same resolution rules) so you can resolve and invoke your
dependencies from plain Python: CLI scripts, workers, cron jobs, tests — no HTTP
server required.

## Install

```bash
pip install fastapi-standalone-di
# or
uv add fastapi-standalone-di
```

## Quick start

Resolve any callable that uses `Depends()`, exactly as FastAPI would:

```python
import asyncio

from fastapi import Depends

from fastapi_standalone_di import FastAPIContainer


def get_settings() -> dict[str, str]:
    return {"db_url": "postgres://localhost/app"}


class Database:
    def __init__(self, settings: dict[str, str] = Depends(get_settings)) -> None:
        self.url = settings["db_url"]


async def main() -> None:
    # Explicit lifetime: create the container, then close it when done.
    container = FastAPIContainer()
    db = await container.get(Database)
    print(db.url)  # postgres://localhost/app
    await container.aclose()

    # Or as an async context manager (auto-closes, runs any yield teardown):
    async with FastAPIContainer() as container:
        db = await container.get(Database)
        print(db.url)


asyncio.run(main())
```

- `container.get(dep)` — resolve one dependency and return its instance.
- `container.resolve(a, b, ...)` — resolve several; returns a `ResolvedDependencies`
  you query with `.get(dep)` / `.optional(dep)`.
- `container.invoke(fn)` — resolve `fn`'s `Depends()` parameters and call it (entry point, not cached).
- `container.scope()` — open a short-lived scope (see [Dependency scopes](#dependency-scopes)).

By default, resolved instances are cached on the container for reuse across calls
(container scope). `await container.aclose()` closes the container and runs any
`yield` teardown.

### `yield` dependencies and teardown

Generator dependencies (sync or async) are supported. Their teardown runs when the
container is closed — use it as an async context manager, or call `await container.aclose()`:

```python
import asyncio
from collections.abc import AsyncIterator

from fastapi_standalone_di import FastAPIContainer


class Client:
    async def close(self) -> None: ...


async def get_client() -> AsyncIterator[Client]:
    client = Client()
    try:
        yield client
    finally:
        await client.close()  # runs on container exit


async def main() -> None:
    async with FastAPIContainer() as container:
        client = await container.get(get_client)
        assert isinstance(client, Client)
    # client.close() has run here


asyncio.run(main())
```

### Overriding dependencies (tests)

```python
from fastapi import Depends

from fastapi_standalone_di import FastAPIContainer


def get_settings() -> dict[str, str]:
    return {"db_url": "postgres://localhost/app"}


class Database:
    def __init__(self, settings: dict[str, str] = Depends(get_settings)) -> None:
        self.url = settings["db_url"]


async def main() -> None:
    container = FastAPIContainer(
        dependency_overrides={get_settings: lambda: {"db_url": "sqlite://"}},
    )
    db = await container.get(Database)
    assert db.url == "sqlite://"
```

### Registrable interfaces

Declare an interface and bind its implementation elsewhere, then depend on the interface:

```python
from abc import ABC, abstractmethod

from fastapi_standalone_di import FastAPIContainer, RegistrableDependency


class IClock(ABC, RegistrableDependency):
    @abstractmethod
    def now(self) -> str: ...


class SystemClock(IClock):
    def now(self) -> str:
        return "2026-07-02"


IClock.register(SystemClock)


async def main() -> None:
    container = FastAPIContainer()
    clock = await container.get(IClock)  # -> SystemClock instance
    print(clock.now())
```

### Sharing application state

`AppState` gives dependencies a unified handle on application-level objects (clients,
caches, …) whether they run inside a request or standalone:

```python
from fastapi import Depends

from fastapi_standalone_di import (
    AppState,
    FastAPIContainer,
    get_app_state,
    set_app_state_value,
)


class Db: ...


def get_db(app_state: AppState = Depends(get_app_state)) -> Db | None:
    db: Db | None = app_state.get("db")
    return db


async def main() -> None:
    set_app_state_value("db", Db())  # at startup
    container = FastAPIContainer()
    db = await container.get(get_db)
    assert db is not None
```

## Dependency scopes

Each dependency has a **scope** that decides its lifetime and when its `yield`
teardown runs:

- **`CONTAINER`** (default) — one instance per container, torn down at `aclose()`.
- **`SCOPED`** — one instance per active scope, torn down when that scope closes.

A scope is opened explicitly with `async with container.scope()`, and implicitly
around `container.invoke(fn)`. Resolving a `SCOPED` dependency outside a scope
raises `ScopeError` — use a scope (or `invoke`) for those.

The scope is configurable globally with `default_scope` and per dependency with
`scopes`; the per-dependency map wins. `default_scope` also accepts a dict
mapping FastAPI's `Depends(scope=...)` literals (`"request"` / `"function"`, plus
`None` for no explicit scope) to a `DependencyScope`.

```python
import asyncio
from collections.abc import AsyncIterator

from fastapi import Depends

from fastapi_standalone_di import DependencyScope, FastAPIContainer


class Session:
    async def close(self) -> None: ...


async def get_session() -> AsyncIterator[Session]:
    session = Session()
    try:
        yield session
    finally:
        await session.close()


class Repository:
    def __init__(self, session: Session = Depends(get_session)) -> None:
        self.session = session


async def handler(repo: Repository = Depends(Repository)) -> Session:
    return repo.session


async def main() -> None:
    # The session and its repository live for one scope; anything else stays
    # container-scoped (a singleton per container).
    container = FastAPIContainer(
        scopes={
            get_session: DependencyScope.SCOPED,
            Repository: DependencyScope.SCOPED,
        },
    )

    async with container.scope() as scope:
        repo = await scope.get(Repository)
        assert isinstance(repo.session, Session)
    # session.close() has run here, at scope exit

    await container.invoke(handler)  # opens a scope implicitly around the call
    # the session used by handler is closed once invoke() returns

    await container.aclose()


asyncio.run(main())
```

Orthogonally to the scope, FastAPI's `use_cache` (default `True`) controls whether
an instance is shared between consumers within a scope or created fresh at each
injection point. A `yield` dependency's resources stay open until the scope that
owns it closes — so a fresh (`use_cache=False`) generator at `CONTAINER` scope is
held until `aclose()`; put transient resources in a `SCOPED` scope sized as one
unit of work.

## How it works

The container asks FastAPI for the dependency tree of your callable
(`fastapi.dependencies.utils.get_dependant`), resolves sub-dependencies
recursively, and invokes each callable with the right execution model
(coroutine, sync in a threadpool, sync/async generator via an `AsyncExitStack`).
Each dependency is cached and torn down on the exit stack of its scope — the
container's for `CONTAINER`, the resolution scope's for `SCOPED`.
Request/header/query/cookie/path parameters — which don't exist outside ASGI —
fall back to a stub `Request` and their declared defaults.

## Requirements

- Python ≥ 3.12
- FastAPI ≥ 0.61

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

## License

[MIT](./LICENSE) © Rémi Alvergnat
