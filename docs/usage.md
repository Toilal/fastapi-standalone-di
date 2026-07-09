Usage
=====

`fastapi-standalone-di` centres on one object: the
[`FastAPIContainer`](./api.md#fastapicontainer). It holds the configuration
needed to resolve a FastAPI dependency tree without a running ASGI application,
and owns the lifetime of the instances it builds.

Resolving dependencies outside ASGI
-----------------------------------

The container asks FastAPI for the dependency tree of your callable
(`fastapi.dependencies.utils.get_dependant`), resolves sub-dependencies
recursively, and invokes each callable with the right execution model
(coroutine, sync function in a threadpool, sync/async generator via an
`AsyncExitStack`). Every resolution entry point is asynchronous:

```python
import asyncio

from fastapi import Depends

from fastapi_standalone_di import FastAPIContainer


class Config:
    url = "postgres://localhost/app"


class Database:
    def __init__(self, config: Config = Depends(Config)) -> None:
        self.config = config


async def main() -> None:
    async with FastAPIContainer() as container:
        # get() returns one instance directly:
        db = await container.get(Database)
        assert db.config.url == "postgres://localhost/app"

        # resolve() takes several dependencies and returns a bag:
        deps = await container.resolve(Database, Config)
        assert deps.get(Database) is db
        assert deps.get(Config) is db.config


asyncio.run(main())
```

`resolve()` returns a [`ResolvedDependencies`](./api.md#resolveddependencies)
whose `get(dep)` / `optional(dep)` address the dependencies you explicitly asked
for. The sub-dependencies resolved along the way are captured too: reach them by
passing `transitive=True`, or iterate over the complete set with
`all_instances()` (a read-only mapping, ordered sub-dependencies first). The
instances returned are exactly those wired into their dependents.

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

Invoking an entry point
-----------------------

`invoke(fn)` resolves `fn`'s `Depends()` parameters and calls it. Unlike
`resolve`/`get`, the entry point itself is **not** cached — it is treated as a
one-shot call. It runs inside an implicit resolution scope, so any `SCOPED`
dependency it uses is torn down once `fn` returns (see
[Dependency scopes](#dependency-scopes)).

```python
import asyncio

from fastapi import Depends

from fastapi_standalone_di import FastAPIContainer


def get_greeting() -> str:
    return "hello"


async def handler(greeting: str = Depends(get_greeting)) -> str:
    return greeting.upper()


async def main() -> None:
    async with FastAPIContainer() as container:
        assert await container.invoke(handler) == "HELLO"


asyncio.run(main())
```

`invoke_resolved(fn)` gives the same result plus the resolved bag: `get(fn)` is
the invocation result, and the sub-dependencies are exposed the same way as with
`resolve`.

Caching
-------

Resolved instances are cached, keyed by the resolved callable, within the scope
that owns them. The default scope is the **container** itself, so `get`,
`resolve` and their sub-dependencies reuse the same instance across every call
until `await container.aclose()` — or `container.clear_cache()`, which drops the
cached instances without running teardown.

Caching is per injection *within* a scope, so two consumers of the same
dependency share one instance. FastAPI's `use_cache=False` on a `Depends(...)`
opts that dependency out: it is rebuilt fresh at each injection point, while its
`yield` teardown still runs on its scope's exit stack.

`yield` dependencies and teardown
---------------------------------

Generator dependencies (sync or async) are supported. Their teardown runs when
the owning scope closes — for `CONTAINER` scope, that is when the container is
closed. Use the container as an async context manager, or call
`await container.aclose()`:

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

Each dependency is entered on the `AsyncExitStack` of its scope — the container's
for `CONTAINER`, the resolution scope's for `SCOPED` — so teardown always runs in
reverse resolution order when that scope closes.

Overriding dependencies
-----------------------

Pass `dependency_overrides` to swap an implementation, mirroring FastAPI's
`app.dependency_overrides`. This is the idiomatic way to inject test doubles:

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
    container = FastAPIContainer(
        dependency_overrides={get_settings: lambda: {"db_url": "sqlite://"}},
    )
    db = await container.get(Database)
    assert db.url == "sqlite://"


asyncio.run(main())
```

Registrable interfaces
----------------------

[`RegistrableDependency`](./api.md#registrabledependency) lets you declare an
*interface* (an abstract base) and bind its concrete implementation elsewhere,
then depend on the interface. The container dereferences the interface to its
registered implementation automatically:

```python
import asyncio
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
    assert clock.now() == "2026-07-02"


asyncio.run(main())
```

Register with `IClock.register(SystemClock)`, clear it with
`IClock.register(None)`. Resolving an interface with no implementation registered
raises `RuntimeError`.

!!! note "Patching FastAPI's `Depends`"
    The container resolves the interface indirection on its own. You only need
    [`patch_for_registrable_dependency_support()`](./api.md#patch_for_registrable_dependency_support)
    when FastAPI itself must see the concrete implementation at introspection
    time (e.g. for OpenAPI generation inside a real app). It patches the
    `Depends` class in place, so it affects every `Depends()` object regardless
    of when it was created — calling it once at import time is enough and its
    order relative to your `@singleton`/`Depends()` definitions does not matter.

Discovering bindings
--------------------

In a feature-oriented codebase, each feature binds its interfaces to their
implementations in a per-feature `di` module:

<!-- docs-test: skip -->
```python
# myapp/features/orders/di.py
def register() -> None:
    OrderService.register(DefaultOrderService)
    OrderRepository.register(SqlOrderRepository)
```

Because FastAPI resolves a route's full `Depends(...)` tree at decoration time,
every binding — including cross-feature ones — must be in place *before* the
routers are mounted. [`register_bindings`](./api.md#register_bindings) walks the
subpackages of a package, imports each one's `di` module and calls its
`register()`, so the whole wiring happens up front in one order-independent call:

```python
from fastapi_standalone_di import register_bindings

register_bindings("myapp.features")  # before include_router / router discovery
```

A subpackage with no `di` module is skipped silently (a feature may declare no
bindings); a `di` module that exposes no callable `register` is surfaced with a
`logging.warning` instead of failing at request time. Point it at several feature
roots at once with `register_bindings(pkg_a, pkg_b)`, nest the binding module with
`module="api.di"`, rename the callable with `attr=...`, or cover nested feature
trees with `recursive=True`.

Each package you pass wires its **own** `di` module too, not just its
subpackages — so an entry point that needs only a couple of features can wire
exactly those instead of a whole subtree:

```python
from fastapi_standalone_di import register_bindings

# web entry point: every feature under the root
register_bindings("myapp.features")

# daemon entry point: only these two features
register_bindings("myapp.features.config", "myapp.features.source")
```

Overlapping targets (e.g. a root and one of its children) each bind at most
once.

Auto-wiring bindings by convention
----------------------------------

When each interface has exactly one implementation that already subclasses it,
even the per-feature `di` module is boilerplate. [`auto_bindings`](./api.md#auto_bindings)
derives the wiring from the class hierarchy: it scans packages for interface
classes (those carrying `RegistrableDependency` as a **direct** base) and for
implementation classes, then binds each interface to the implementation that
declares it as a direct base.

```python
from fastapi_standalone_di import Binding, RegistrableDependency, auto_bindings


class ICache(RegistrableDependency): ...


class RedisCache(ICache):  # discovered as the implementation of ICache
    ...


bindings: list[Binding] = auto_bindings("myapp")
for binding in bindings:
    verb = "kept" if binding.already_bound else "bound"
    print(verb, binding.interface.__qualname__)
```

Pass one set of packages positionally (they may hold both interfaces and
implementations), or split the roles with `interfaces=[...]` and
`implementations=[...]`. An interface that already carries an implementation is
left untouched and reported with `already_bound=True`; one with no match, or an
unresolved ambiguity, raises `AutoBindingError` **without registering anything**
(resolution and registration are separate phases). Scanning is recursive by
default, and — unlike `register_bindings` — imports every module in the scanned
packages to inspect their classes, so call it once at bootstrap.

When several implementations match one interface, pass a `conflict_solver` to
break the tie; it is called once per conflicting interface and returns the chosen
candidate (or `None` to leave the ambiguity as an error):

```python
from fastapi_standalone_di import RegistrableDependency, auto_bindings


def prefer_primary(
    interface: type[RegistrableDependency], impls: list[type]
) -> type | None:
    primary = [impl for impl in impls if impl.__module__.startswith("myapp.primary")]
    return primary[0] if len(primary) == 1 else None


auto_bindings("myapp", conflict_solver=prefer_primary)
```

An implementation may be decorated with [`singleton`](#singleton): `auto_bindings`
discovers it through the class it wraps and registers the wrapper, so the
application-lifetime cache survives instead of being bound away.

```python
from fastapi_standalone_di import RegistrableDependency, auto_bindings, singleton


class ICache(RegistrableDependency): ...


@singleton
class RedisCache(ICache):
    def __init__(self) -> None: ...


auto_bindings("myapp")  # ICache -> the singleton wrapper of RedisCache
```

Only the default (eager) mode is wired this way. A lazy `singleton` delegates to
`container.get(factory)`, which re-dereferences the implementation class back
through the interface it subclasses — a cycle — so `auto_bindings` rejects a lazy
implementation with an `AutoBindingError`. Keep an implementation-class singleton
eager, or register a lazy singleton *factory function* by hand.

Sharing application state
-------------------------

[`AppState`](./api.md#appstate) gives dependencies a unified handle on
application-level objects (clients, caches, …) whether they run inside a request
or standalone. In FastAPI mode it delegates to `request.app.state`; standalone it
falls back to a module-level singleton store.

```python
import asyncio

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


asyncio.run(main())
```

Set values at startup with `set_app_state_value(key, value)` — this writes to the
standalone singleton so the value is available to both FastAPI and standalone
contexts. Inside a dependency, obtain the `AppState` by depending on
`get_app_state`. To back the container's state with a real application instead,
pass `app_state=AppState.from_app(app)`.

The [`get_container`](./api.md#get_container) dependency completes the picture:
register a container in `app_state` under `"container"` and any dependency can
retrieve the active container by depending on `get_container`.

Singletons
----------

[`singleton`](./api.md#singleton) turns a dependency factory into an
application-lifetime singleton: its instance is built lazily on first access,
cached in the [`AppState`](./api.md#appstate), and reused thereafter. Because the
store is the `AppState`, the same instance is shared **across requests** in ASGI
(via `request.app.state`) and **across containers** standalone (when they share an
`AppState`) — the cache gate lives inside the dependency both engines invoke.

```python
import asyncio

from fastapi import Depends

from fastapi_standalone_di import FastAPIContainer, singleton


class Settings:
    url = "postgres://localhost/app"


class Database:
    def __init__(self, url: str) -> None:
        self.url = url


@singleton(key="db")
def get_db(settings: Settings = Depends(Settings)) -> Database:
    return Database(settings.url)  # body runs once; result cached in app_state["db"]


async def main() -> None:
    async with FastAPIContainer() as container:
        first = await container.get(get_db)
        second = await container.get(get_db)
        assert first is second
        assert first.url == "postgres://localhost/app"


asyncio.run(main())
```

Use it with `Depends(get_db)` in a route or `container.get(get_db)` standalone —
same instance either way. `key` names the `AppState` entry; sharing it with
`set_app_state_value("db", ...)` presets the singleton (the preset short-circuits
construction). Omit `key` for a namespaced, collision-free default.

By default (**eager** mode) the factory *body* runs once, while its `Depends(...)`
sub-tree is re-resolved on each access — fine for cheap sub-dependencies; make
expensive ones singletons too (it composes). Eager mode rejects generator
factories: there is no application-lifetime owner for their teardown.

For a `yield` singleton (or to resolve the sub-tree exactly once), pass
`lazy=True`. Construction is then delegated to the container reachable via
[`get_container`](./api.md#get_container), which owns the teardown and runs it at
`aclose()` — application shutdown:

```python
import asyncio
from collections.abc import AsyncIterator

from fastapi_standalone_di import FastAPIContainer, singleton


class Client:
    async def close(self) -> None: ...


@singleton(key="client", lazy=True)
async def get_client() -> AsyncIterator[Client]:
    client = Client()
    try:
        yield client
    finally:
        await client.close()  # runs at container close == app shutdown


async def main() -> None:
    async with FastAPIContainer() as container:
        first = await container.get(get_client)
        second = await container.get(get_client)
        assert first is second
    # client.close() has run here


asyncio.run(main())
```

In ASGI, a `lazy=True` singleton needs a container registered in `app_state` to
build its value. The easiest way is the
[`container_lifespan`](./api.md#container_lifespan) helper, which installs one at
startup and closes it (running the singletons' `yield` teardown) at shutdown:

<!-- docs-test: skip -->
```python
from fastapi import FastAPI
from fastapi_standalone_di import container_lifespan

app = FastAPI(lifespan=container_lifespan)
```

To compose it with your own startup/shutdown, wrap it:

<!-- docs-test: skip -->
```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    async with container_lifespan(app):
        ...  # your own startup
        yield
```

If you register the container yourself instead
(`app.state.container = FastAPIContainer(app_state=AppState.from_app(app))`),
remember to call `await container.aclose()` at shutdown so `yield` teardown runs.

A container is *not* required when the value is **preset** under the singleton's
key (`app.state.<key> = value`, or `set_app_state_value(key, value)`): the preset
short-circuits construction before any container is needed. Standalone, the
resolving container always provides itself.

Dependency scopes
-----------------

Each dependency has a [`DependencyScope`](./api.md#dependencyscope) that decides
its lifetime and when its `yield` teardown runs:

- **`CONTAINER`** (default) — one instance per container, torn down at
  `aclose()`.
- **`SCOPED`** — one instance per active scope, torn down when that scope closes.

A scope is opened explicitly with `async with container.scope()`, and implicitly
around `container.invoke(fn)`. Resolving a `SCOPED` dependency outside a scope
raises [`ScopeError`](./api.md#scopeerror) — use a scope (or `invoke`) for those.

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

A `CONTAINER`-scoped dependency cannot depend on a `SCOPED` one: the container
would capture an instance torn down at scope close (a captive dependency), so the
container raises `ScopeError`. Make the dependent `SCOPED` too.

Orthogonally to the scope, FastAPI's `use_cache` (default `True`) controls
whether an instance is shared between consumers within a scope or created fresh at
each injection point. A `yield` dependency's resources stay open until the scope
that owns it closes — so a fresh (`use_cache=False`) generator at `CONTAINER`
scope is held until `aclose()`; put transient resources in a `SCOPED` scope sized
as one unit of work.

Concurrency
-----------

Resolution is concurrency-safe: several `get`/`resolve`/`invoke` calls may run
concurrently on the same container (e.g. under `asyncio.gather`). Concurrent
resolutions of the same shared dependency are serialised, so a cache miss still
yields a single instance registered once on its exit stack — no duplicate
instance and no double teardown. Independent dependencies are not serialised
against each other; the lock only guards same-key construction, not parallel
throughput.
