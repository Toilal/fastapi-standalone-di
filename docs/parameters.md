Parameters & connection
========================

FastAPI dependencies routinely reach for things that only exist inside an ASGI
request: `Query`/`Path`/`Header`/`Cookie` parameters, the `Request` object, a
`Response` to mutate, `BackgroundTasks`, or `SecurityScopes`. Standalone there is
no transport, so the container supplies each of these from its own configuration.

Supplying query / path / header / cookie parameters
---------------------------------------------------

Dependencies often declare `Query`, `Path`, `Header` or `Cookie` parameters.
These arrive over the wire as **strings** in a real request; standalone, you
supply them the same way — as strings, per source — and FastAPI coerces each to
its declared type (raising a clear error on an incompatible value):

```python
import asyncio

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


asyncio.run(main())
```

Each source (`query`, `path`, `headers`, `cookies`) accepts either a bare
`{name: value}` mapping or a [`ParamSource`](./api.md#paramsource)`(values=...,
default=...)`. Resolution, per parameter, is:

1. an explicit value (by name, then alias),
2. the parameter's own declared default,
3. the source-wide `default` string,
4. otherwise a [`MissingParameterError`](./api.md#parametererror-hierarchy) for a
   required parameter left unsupplied.

The source-wide `default` only fills **required** parameters — it never overrides
a parameter's declared default:

```python
import asyncio

from fastapi import Query

from fastapi_standalone_di import FastAPIContainer, ParamSource


async def handler(a: int = Query(...), b: str = Query(...)) -> tuple[int, str]:
    return a, b


async def main() -> None:
    # One fallback string, coerced per declared type.
    container = FastAPIContainer(query=ParamSource(default="0"))
    assert await container.invoke(handler) == (0, "0")


asyncio.run(main())
```

A supplied string incompatible with the declared type raises
[`ParameterValidationError`](./api.md#parametererror-hierarchy) before the value
ever reaches the callable.

The standalone `Request`
------------------------

A dependency may declare `request: Request` (or `HTTPConnection`). Outside ASGI
there is no live connection, so the container injects a **stub `Request`** built
per resolution operation — one `get`/`invoke`/`resolve` call — and shared across
that call's whole dependency tree, exactly as a real request is shared by all
dependencies of one HTTP request. Separate operations get separate requests, so
nothing leaks between them. The stub is built lazily, only when a dependency
actually declares such a parameter: a tree without one builds no request at all.

```python
import asyncio

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


asyncio.run(main())
```

What the stub supports:

- `request.query_params`, `request.path_params`, `request.cookies` — mirror the
  container's `query=` / `path=` / `cookies=` configuration.
- `request.app.state` — reflects the container's `AppState` (shared storage). Pass
  `app=your_fastapi_app` to make `request.app` your real application instead.
- `await request.body()` returns `b""`; `request.client` is `None`; `scheme`,
  `server`, `http_version` carry neutral standalone defaults.
- `request.state` is a per-operation scratchpad, shared across the dependency tree
  of one call and reset for the next.

There is no transport: header values are best supplied through typed `Header`
parameters (above) rather than read from `request.headers`, and response
mutations have no effect.

`Response` and `BackgroundTasks`
--------------------------------

A dependency declaring `response: Response` receives a fresh stub whose
header/cookie/status mutations are accepted but have no transport effect (nothing
sends it).

A dependency declaring `background_tasks: BackgroundTasks` receives a real
`BackgroundTasks`. Tasks added with `add_task(...)` run when the owning scope
closes — `aclose()` for `CONTAINER`, scope exit for `SCOPED`:

```python
import asyncio

from fastapi import BackgroundTasks

from fastapi_standalone_di import FastAPIContainer

ran: list[str] = []


async def handler(background_tasks: BackgroundTasks) -> None:
    background_tasks.add_task(ran.append, "done")


async def main() -> None:
    async with FastAPIContainer() as container:
        await container.invoke(handler)
        # invoke() opens an implicit scope; the task has run by the time it returns
    assert ran == ["done"]


asyncio.run(main())
```

Security scopes
---------------

A dependency declaring `scopes: SecurityScopes` is served from the container's
`security_scopes=` configuration — supplied the same way as query/header/cookie
values, since standalone there is no security-scheme chain to accumulate scopes
from:

```python
import asyncio

from fastapi.security import SecurityScopes

from fastapi_standalone_di import FastAPIContainer


async def handler(scopes: SecurityScopes) -> list[str]:
    return scopes.scopes


async def main() -> None:
    container = FastAPIContainer(security_scopes=["me", "items"])
    assert await container.invoke(handler) == ["me", "items"]


asyncio.run(main())
```

The value is global to the container (empty when unset) — the per-branch scopes a
parent grants via `Security(dep, scopes=[...])` are not reconstructed, since
standalone has no request chain to accumulate them along.

Authentication is not enforced (there is no transport): a security scheme such as
`OAuth2PasswordBearer` still runs as an ordinary dependency and reads the stub
`Request`, so supply an `Authorization` header via `headers={...}` if you want it
to succeed.
