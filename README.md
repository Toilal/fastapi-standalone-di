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

### Caching

Resolved instances are cached, keyed by the resolved callable, within the scope
that owns them. The default scope is the **container** itself, so `get`, `resolve`
and their sub-dependencies reuse the same instance across every call until
`await container.aclose()` (or `container.clear_cache()`, which drops the cached
instances without running teardown). `SCOPED` dependencies are cached per open
scope instead (see [Dependency scopes](#dependency-scopes)), and `invoke(fn)`
never caches `fn` itself — it is treated as a one-shot entry point.

Caching is per injection *within* a scope, so two consumers of the same
dependency share one instance. FastAPI's `use_cache=False` on a `Depends(...)`
opts that dependency out: it is rebuilt fresh at each injection point, while its
`yield` teardown still runs on its scope's exit stack. `await container.aclose()`
closes the container and runs any `yield` teardown.

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

## Supplying query / path / header / cookie parameters

Dependencies often declare `Query`, `Path`, `Header` or `Cookie` parameters.
These arrive over the wire as **strings** in a real request; standalone, you
supply them the same way — as strings, per source — and FastAPI coerces each to
its declared type (raising a clear error on an incompatible value):

```python
from fastapi import Path, Query

from fastapi_standalone_di import FastAPIContainer, ParamSource


async def handler(
    user_id: int = Path(...),
    limit: int = Query(10),
    q: str = Query(...),
) -> tuple[int, int, str]:
    return user_id, limit, q


async def main() -> None:
    container = FastAPIContainer(
        path={"user_id": "42"},                    # dict shorthand: values only
        query=ParamSource(values={"q": "hello"}),  # "10" default for limit is kept
    )
    assert await container.invoke(handler) == (42, 10, "hello")
```

Each source (`query`, `path`, `headers`, `cookies`) accepts either a bare
`{name: value}` mapping or a `ParamSource(values=..., default=...)`. Resolution,
per parameter, is: an explicit value (by name, then alias) → the parameter's own
declared default → the source-wide `default` string → otherwise a
`MissingParameterError` for a required parameter left unsupplied. The
source-wide `default` only fills **required** parameters — it never overrides a
parameter's declared default:

```python
from fastapi import Query

from fastapi_standalone_di import FastAPIContainer, ParamSource


async def handler(a: int = Query(...), b: str = Query(...)) -> tuple[int, str]:
    return a, b


async def main() -> None:
    # One fallback string, coerced per declared type.
    container = FastAPIContainer(query=ParamSource(default="0"))
    assert await container.invoke(handler) == (0, "0")
```

### The standalone `Request`

A dependency may declare `request: Request` (or `HTTPConnection`). Outside ASGI
there is no live connection, so the container injects a **stub `Request`** built
per resolution operation — one `get`/`invoke`/`resolve` call — and shared across
that call's whole dependency tree, exactly as a real request is shared by all
dependencies of one HTTP request. Separate operations get separate requests, so
nothing leaks between them.

```python
from fastapi import Request

from fastapi_standalone_di import FastAPIContainer, set_app_state_value


async def handler(request: Request) -> dict[str, object]:
    return {
        "limit": request.query_params.get("limit"),  # from query=
        "db": request.app.state.db,                   # from app_state
        "body": await request.body(),                 # b"" standalone
    }


async def main() -> None:
    set_app_state_value("db", "the-db")
    container = FastAPIContainer(query={"limit": "10"})
    assert await container.invoke(handler) == {
        "limit": "10",
        "db": "the-db",
        "body": b"",
    }
```

What the stub supports:

- `request.query_params`, `request.path_params`, `request.cookies` — mirror the
  container's `query=` / `path=` / `cookies=` configuration.
- `request.app.state` — reflects the container's `AppState` (shared storage). Pass
  `app=your_fastapi_app` to make `request.app` your real application instead.
- `await request.body()` returns `b""`; `request.client` is `None`; `scheme`,
  `server`, `http_version` carry neutral standalone defaults.
- `request.state` is a per-operation scratchpad, shared across the dependency
  tree of one call and reset for the next.

There is no transport: header values are best supplied through typed `Header`
parameters (see above) rather than read from `request.headers`, and response
mutations have no effect.

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
Connection objects (`Request`/`HTTPConnection`) are served by a stub built once
per resolution operation and shared across its dependency tree (see
[The standalone `Request`](#the-standalone-request)). Header, query,
path and cookie parameters — which don't exist outside ASGI — are supplied from
the container's per-source configuration (as strings, coerced by FastAPI to the
declared type), then their declared defaults, then a required-parameter error;
see [Supplying query / path / header / cookie parameters](#supplying-query--path--header--cookie-parameters).
A dependency
declaring `response: Response` receives a fresh stub whose header/cookie/status
mutations are accepted but have no transport effect (nothing sends it). A
dependency declaring `background_tasks: BackgroundTasks` receives a real
`BackgroundTasks`; tasks added with `add_task(...)` run when the owning scope
closes (`aclose()` for `CONTAINER`, scope exit for `SCOPED`).

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
