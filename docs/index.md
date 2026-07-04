fastapi-standalone-di
=====================

Use [FastAPI](https://fastapi.tiangolo.com/)'s dependency injection **outside of
any web/ASGI context**.

FastAPI ships a powerful dependency injection system — `Depends`, sub-dependencies,
`yield` teardown, per-resolution caching. It is, however, tightly coupled to the
request/response cycle. `fastapi-standalone-di` reuses that exact machinery
(`get_dependant`, the same resolution rules) so you can resolve and invoke your
dependencies from plain Python: CLI scripts, workers, cron jobs, tests — no HTTP
server required.

Install
-------

```bash
pip install fastapi-standalone-di
```

Or add it to your project with [uv](https://docs.astral.sh/uv/):

```bash
uv add fastapi-standalone-di
```

Requirements:

- Python ≥ 3.12
- FastAPI ≥ 0.61

Quick start
-----------

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

The container exposes a small surface:

- `container.get(dep)` — resolve one dependency and return its instance.
- `container.optional(dep)` — like `get`, but returns `None` when not resolved.
- `container.resolve(a, b, ...)` — resolve several; returns a `ResolvedDependencies`
  you query with `.get(dep)` / `.optional(dep)`.
- `container.invoke(fn)` — resolve `fn`'s `Depends()` parameters and call it
  (entry point, not cached).
- `container.invoke_resolved(fn)` — like `invoke`, but returns a
  `ResolvedDependencies` exposing the sub-dependencies too.
- `container.scope()` — open a short-lived scope (see [Usage](./usage.md#dependency-scopes)).

Read on
-------

- [Usage](./usage.md) — resolving outside ASGI, caching, `yield` teardown,
  overrides, registrable interfaces, application state, and dependency scopes.
- [Parameters & connection](./parameters.md) — supplying query/path/header/cookie
  values, the standalone `Request`, `Response`, `BackgroundTasks` and
  `SecurityScopes`.
- [API reference](./api.md) — every public symbol, its signature and behaviour.

Support
-------

This project is hosted on [GitHub](https://github.com/Toilal/fastapi-standalone-di).
Feel free to open an issue if you think you have found a bug or something is
missing.

License
-------

`fastapi-standalone-di` is licensed under the
[MIT license](https://github.com/Toilal/fastapi-standalone-di/blob/develop/LICENSE).
