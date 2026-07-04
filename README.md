# fastapi-standalone-di

Use [FastAPI](https://fastapi.tiangolo.com/)'s dependency injection **outside of any web/ASGI context**.

FastAPI ships a powerful dependency injection system — `Depends`, sub-dependencies,
`yield` teardown, per-resolution caching. It is, however, tightly coupled to the
request/response cycle. `fastapi-standalone-di` reuses that exact machinery
(`get_dependant`, the same resolution rules) so you can resolve and invoke your
dependencies from plain Python: CLI scripts, workers, cron jobs, tests — no HTTP
server required.

## Documentation

Full documentation is published at
**<https://toilal.github.io/fastapi-standalone-di>**. The in-development docs
(built from `develop`) are previewed at
<https://toilal.github.io/fastapi-standalone-di/dev/>.

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
- `container.invoke_resolved(fn)` — like `invoke`, but returns a `ResolvedDependencies` exposing the sub-dependencies too (see [Inspecting resolved sub-dependencies](#inspecting-resolved-sub-dependencies)).
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

### Inspecting resolved sub-dependencies

`resolve()` returns a `ResolvedDependencies` whose `get(dep)` / `optional(dep)`
address the dependencies you explicitly asked for. The sub-dependencies resolved
along the way are captured too: reach them by passing `transitive=True`, or
iterate over the complete set with `all_instances()` (a read-only mapping,
ordered sub-dependencies first). The instances returned are exactly those wired
into their dependents.

```python
import asyncio

from fastapi import Depends

from fastapi_standalone_di import FastAPIContainer


class Config:
    url = "postgres://localhost/app"


class Database:
    def __init__(self, config: Config = Depends(Config)) -> None:
        self.config = config


class Service:
    def __init__(self, db: Database = Depends(Database)) -> None:
        self.db = db


async def main() -> None:
    async with FastAPIContainer() as container:
        deps = await container.resolve(Service)

        # get()/optional() address only the dependency you asked for:
        service = deps.get(Service)

        # sub-dependencies resolved along the way need transitive=True:
        db = deps.get(Database, transitive=True)
        config = deps.optional(Config, transitive=True)
        assert db is service.db
        assert config is service.db.config

        # or iterate over every instance that was built:
        assert set(deps.all_instances()) == {Service, Database, Config}


asyncio.run(main())
```

`invoke_resolved(fn)` gives the same bag for an entry-point call: `get(fn)` is
the invocation result, and the sub-dependencies are exposed the same way. As
with `invoke`, `SCOPED` dependencies are torn down before it returns — the bag
still references them, but their `yield` teardown has already run.

A dependency injected with `use_cache=False` is rebuilt at each injection point;
only its last-built instance is retained under its callable key.

### Concurrency

Resolution is concurrency-safe: several `get`/`resolve`/`invoke` calls may run
concurrently on the same container (e.g. under `asyncio.gather`). Concurrent
resolutions of the same shared dependency are serialised, so a cache miss still
yields a single instance registered once on its exit stack — no duplicate
instance and no double teardown. Independent dependencies are not serialised
against each other; the lock only guards same-key construction, not parallel
throughput.

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

A dependency declaring `scopes: SecurityScopes` is served the same way: pass
`security_scopes=["me", "items"]` to the container and it receives a
`SecurityScopes` carrying those scopes (empty when unset). The value is global
to the container — the per-branch scopes a parent grants via
`Security(dep, scopes=[...])` are not reconstructed, since standalone has no
request chain to accumulate them along.

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
nothing leaks between them. The stub is built lazily, only when a dependency
actually declares such a parameter: a tree without one builds no request at all.

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
closes (`aclose()` for `CONTAINER`, scope exit for `SCOPED`). A dependency
declaring `scopes: SecurityScopes` receives a `SecurityScopes` built from the
container's `security_scopes=` configuration (empty by default) — supplied the
same way as query/header/cookie values, since standalone there is no
security-scheme chain to accumulate scopes from. Authentication is not enforced
(there is no transport): a security scheme such as `OAuth2PasswordBearer` still
runs as an ordinary dependency and reads the stub `Request`, so supply an
`Authorization` header via `headers={...}` if you want it to succeed.

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
