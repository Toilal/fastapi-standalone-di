API reference
=============

Every public symbol is exported from the top-level `fastapi_standalone_di`
package:

```python
from fastapi_standalone_di import (
    AppState,
    DependantCache,
    DependencyOverrides,
    DependencyScope,
    FastAPIContainer,
    MissingParameterError,
    ParamSource,
    ParameterError,
    ParameterValidationError,
    RegistrableDependency,
    ResolutionScope,
    ResolvedDependencies,
    ScopeError,
    get_app_state,
    get_container,
    patch_for_registrable_dependency_support,
    register_bindings,
    set_app_state_value,
)
```

Container
---------

### FastAPIContainer

Dependency container that resolves FastAPI dependencies outside ASGI. It
encapsulates the configuration for a whole dependency tree and owns the lifetime
of the instances it builds.

```python
FastAPIContainer(
    app_state: AppState | None = None,
    dependency_overrides: DependencyOverrides | None = None,
    dependant_cache: DependantCache | bool = True,
    default_scope: DependencyScope | dict[str | None, DependencyScope] = DependencyScope.CONTAINER,
    scopes: dict[Callable[..., Any], DependencyScope] | None = None,
    app: Any | None = None,
    query: ParamSource | Mapping[str, str] | None = None,
    path: ParamSource | Mapping[str, str] | None = None,
    headers: ParamSource | Mapping[str, str] | None = None,
    cookies: ParamSource | Mapping[str, str] | None = None,
    security_scopes: Sequence[str] | None = None,
)
```

Constructor arguments:

- `app_state` — the [`AppState`](#appstate) backing `get_app_state` and the stub
  request's `app.state`. Defaults to the standalone singleton.
- `dependency_overrides` — a `{callable: replacement}` map, like FastAPI's
  `app.dependency_overrides`.
- `dependant_cache` — a shared [`DependantCache`](#dependantcache), or `True`
  (default) to create one, or `False` to disable introspection caching.
- `default_scope` — the global [`DependencyScope`](#dependencyscope), or a dict
  mapping FastAPI's `Depends(scope=...)` literals (`"request"` / `"function"` /
  `None`) to a scope.
- `scopes` — per-dependency scope overrides; keyed by the declared callable or its
  resolved implementation. Wins over `default_scope`.
- `app` — a real FastAPI/Starlette application to expose as `request.app` on the
  stub request. When unset, a minimal stub app backed by `app_state` is used.
- `query` / `path` / `headers` / `cookies` — connection-parameter sources; each a
  [`ParamSource`](#paramsource) or a bare `{name: value}` mapping. See
  [Parameters & connection](./parameters.md).
- `security_scopes` — the scopes exposed to any `SecurityScopes` parameter.

Resolution methods (all coroutines):

- `await get(dependency)` — resolve a single dependency and return its instance.
- `await optional(dependency)` — resolve a single dependency, or `None` if not
  resolved.
- `await resolve(*dependencies)` — resolve one or more dependencies at container
  scope; returns [`ResolvedDependencies`](#resolveddependencies). Resolving a
  `SCOPED` dependency here raises [`ScopeError`](#scopeerror).
- `await invoke(call)` — resolve `call`'s `Depends()` parameters and invoke it,
  inside an implicit resolution scope. The entry point is not cached.
- `await invoke_resolved(call)` — like `invoke`, but returns the
  [`ResolvedDependencies`](#resolveddependencies); `get(call)` is the invocation
  result.

Scope and lifetime:

- `scope()` — return a [`ResolutionScope`](#resolutionscope) to use as
  `async with container.scope() as scope: ...`.
- `clear_cache()` — drop all cached container-scoped instances without running
  teardown (the `get_app_state` seed is restored).
- `await aclose()` — close the container, running teardown for `CONTAINER`
  `yield` dependencies and any queued `BackgroundTasks`.

The container is an async context manager: `async with FastAPIContainer() as
container:` calls `aclose()` on exit.

### ResolutionScope

A short-lived resolution scope owning `SCOPED` dependency lifetimes. Obtained from
[`FastAPIContainer.scope`](#fastapicontainer) and used as an async context
manager; `SCOPED` dependencies resolved through it are torn down when the block
exits, while `CONTAINER` dependencies are delegated to the parent container and
outlive the scope.

It offers the same resolution surface as the container: `await get(dependency)`,
`await optional(dependency)`, `await resolve(*dependencies)`,
`await invoke(call)`, `await invoke_resolved(call)`.

### ResolvedDependencies

The bag returned by `resolve` / `invoke_resolved`.

- `get(dependency, *, transitive=False)` — retrieve a resolved dependency by its
  type or callable. By default only the top-level dependencies (those explicitly
  passed to `resolve`) are addressable; pass `transitive=True` to reach a
  sub-dependency resolved along the way. Raises `KeyError` if not resolved.
- `optional(dependency, *, transitive=False)` — as `get`, but returns `None`
  instead of raising.
- `all_instances()` — a read-only `Mapping` of every instance resolved in the
  operation, keyed by resolved callable and ordered sub-dependencies first.

Dependency scopes
-----------------

### DependencyScope

`Enum` deciding a dependency's lifetime and teardown boundary:

- `DependencyScope.CONTAINER` — one instance per container, torn down at
  `aclose()`.
- `DependencyScope.SCOPED` — one instance per active scope, torn down when that
  scope closes.

### ScopeError

`RuntimeError` subclass raised on scope misuse: resolving a `SCOPED` dependency
without an active scope, or a `CONTAINER`-scoped dependency depending on a
`SCOPED` one (a captive dependency).

Application state
-----------------

### AppState

Abstraction over Starlette's `State` that works with and without a request. In
**FastAPI mode** reads/writes go to `request.app.state`; in **standalone mode**
they go to a module-level singleton dict.

- `get(key)` / `set(key, value)` / `delete(key)` — read/write/remove a value.
- `as_state()` — return a Starlette `State` sharing this `AppState`'s storage.
- `AppState.from_request(request)` — back it by an ASGI application's state.
- `AppState.from_app(app)` — back it by a Starlette/FastAPI application.
- `AppState.standalone()` — return the module-level singleton.
- `AppState.reset_standalone()` — reset that singleton (useful in tests).

### get_app_state

```python
def get_app_state(request: HTTPConnection = None) -> AppState
```

FastAPI dependency returning an `AppState`. When injected by FastAPI, `request` is
provided and the `AppState` delegates to `request.app.state`; resolved standalone
(e.g. via `FastAPIContainer`) `request` is `None` and the standalone singleton is
used.

### set_app_state_value

```python
def set_app_state_value(key: str, value: Any) -> None
```

Set a value in the standalone `AppState` store. Call it at startup so the value is
available to both FastAPI and standalone contexts.

### get_container

```python
def get_container(app_state: AppState = Depends(get_app_state)) -> FastAPIContainer
```

FastAPI dependency returning the active `FastAPIContainer`. Register the container
in `app_state` at startup, e.g. via
`set_app_state_value("container", FastAPIContainer(...))`. Raises `RuntimeError`
if no container is registered.

Registrable dependencies
------------------------

### RegistrableDependency

Base class for a dependency interface with a swappable implementation. Declare an
abstract interface inheriting `RegistrableDependency`, bind a concrete class with
`Interface.register(Impl)`, and depend on the interface via `Depends(Interface)`
— the container dereferences it to the registered implementation.

- `Interface.register(impl)` — register (or clear, with `None`) the
  implementation.
- `Interface.dependency()` — return the registered implementation; raises
  `RuntimeError` when none is registered.
- `Interface.impl` — class property returning the registered implementation.

### patch_for_registrable_dependency_support

```python
def patch_for_registrable_dependency_support() -> bool
```

Patch `fastapi.params.Depends` so it resolves a `RegistrableDependency` eagerly.
Only needed when FastAPI itself must see the concrete implementation at
introspection time (e.g. for OpenAPI); `FastAPIContainer` does not require it. The
patch only affects `Depends()` objects created afterwards — apply it at import
time. Returns `True` if applied, `False` if already patched.

### register_bindings

```python
def register_bindings(
    *packages: str | ModuleType,
    module: str = "di",
    attr: str = "register",
    recursive: bool = False,
    warn_missing: bool = True,
) -> None
```

Discover per-feature binding modules and run them, so every
`RegistrableDependency` is bound before the routers are mounted. For each
subpackage of every `packages` entry, it imports `<subpackage>.<module>` and
calls its `attr` callable.

- `packages` — the packages to scan, each an imported module or a dotted name.
- `module` — the submodule to look for under each subpackage; may be a dotted
  path (e.g. `"api.di"`).
- `attr` — the callable to invoke on that module.
- `recursive` — also walk nested subpackages, not just the direct ones.
- `warn_missing` — `logging.warning` when a matching module exposes no callable
  `attr`, instead of failing silently at request time.

Subpackages with no such module are skipped silently. An import error raised *by*
a binding module propagates. Raises `ValueError` if a `packages` entry is not a
package.

Connection parameters
---------------------

### ParamSource

```python
@dataclass(frozen=True)
class ParamSource:
    values: Mapping[str, str] = {}
    default: str | None = None
```

How to supply one class of connection parameters (query / path / header /
cookie). `values` are explicit values keyed by parameter name (falling back to
its alias); `default` is a string injected for any **required** parameter of the
source with no explicit value and no declared default. See
[Parameters & connection](./parameters.md).

### ParameterError hierarchy

- `ParameterError` — base class (subclass of `RuntimeError`) for standalone
  connection-parameter resolution errors.
- `MissingParameterError` — a required query/path/header/cookie parameter could
  not be supplied.
- `ParameterValidationError` — a supplied parameter value is incompatible with its
  declared type.

Introspection cache
-------------------

### DependantCache

Cache of FastAPI dependency-tree introspection results, mapping a callable to the
`Dependant` produced by FastAPI's `get_dependant`, so repeated `resolve` calls
skip re-introspecting the same callables. A single instance can be shared across
several containers.

- `get_dependant(call)` / `set_dependant(call, dependant)` — look up / store a
  `Dependant`.
- `clear()` — drop all cached entries.

### DependencyOverrides

Type alias: `dict[Callable[..., Any], Callable[..., Any]]` — the shape accepted by
`FastAPIContainer(dependency_overrides=...)`.
