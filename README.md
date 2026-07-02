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


def get_settings() -> dict:
    return {"db_url": "postgres://localhost/app"}


class Database:
    def __init__(self, settings: dict = Depends(get_settings)) -> None:
        self.url = settings["db_url"]


async def main() -> None:
    async with FastAPIContainer() as container:
        db = await container.get(Database)
        print(db.url)  # postgres://localhost/app


asyncio.run(main())
```

- `container.get(dep)` — resolve one dependency and return its instance.
- `container.resolve(a, b, ...)` — resolve several; returns a `ResolvedDependencies`
  you query with `.get(dep)` / `.optional(dep)`.
- `container.invoke(fn)` — resolve `fn`'s `Depends()` parameters and call it (entry point, not cached).

Resolved instances are cached on the container for reuse across calls.

### `yield` dependencies and teardown

Generator dependencies (sync or async) are supported. Their teardown runs when the
container is closed — use it as an async context manager, or call `await container.aclose()`:

```python
async def get_client():
    client = Client()
    try:
        yield client
    finally:
        await client.close()  # runs on container exit


async with FastAPIContainer() as container:
    client = await container.get(get_client)
    ...
# client.close() has run here
```

### Overriding dependencies (tests)

```python
container = FastAPIContainer(dependency_overrides={get_settings: lambda: {"db_url": "sqlite://"}})
db = await container.get(Database)
```

### Registrable interfaces

Declare an interface and bind its implementation elsewhere, then depend on the interface:

```python
from abc import ABC, abstractmethod
from fastapi_standalone_di import RegistrableDependency


class IClock(ABC, RegistrableDependency):
    @abstractmethod
    def now(self) -> str: ...


class SystemClock(IClock):
    def now(self) -> str:
        return "2026-07-02"


IClock.register(SystemClock)

clock = await container.get(IClock)  # -> SystemClock instance
```

### Sharing application state

`AppState` gives dependencies a unified handle on application-level objects (clients,
caches, …) whether they run inside a request or standalone:

```python
from fastapi_standalone_di import AppState, get_app_state, set_app_state_value

set_app_state_value("db", db_client)  # at startup


def get_db(app_state: AppState = Depends(get_app_state)):
    return app_state.get("db")


db = await container.get(get_db)
```

## How it works

The container asks FastAPI for the dependency tree of your callable
(`fastapi.dependencies.utils.get_dependant`), resolves sub-dependencies
recursively, and invokes each callable with the right execution model
(coroutine, sync in a threadpool, sync/async generator via an `AsyncExitStack`).
Request/header/query/cookie/path parameters — which don't exist outside ASGI —
fall back to a stub `Request` and their declared defaults.

## Requirements

- Python ≥ 3.12
- FastAPI ≥ 0.135

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

## License

[MIT](./LICENSE) © Rémi Alvergnat
