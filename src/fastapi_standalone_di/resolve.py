"""Resolve FastAPI dependencies outside of an ASGI request context.

This module provides :class:`FastAPIContainer` to build and invoke a FastAPI
dependency tree without a running ASGI application. Useful for CLI scripts,
background tasks, tests, or any situation where callables declaring
``Depends()`` (or services registered via ``RegistrableDependency``) need to be
obtained programmatically.

Example usage::

    container = FastAPIContainer()
    deps = await container.resolve(IUserService, IUserRepository)
    service = deps.get(IUserService)
    repo = deps.get(IUserRepository)

    # With custom configuration:
    container = FastAPIContainer(
        app_state=AppState.from_app(app),
        dependency_overrides={get_db: lambda: mock_db},
    )
    service = await container.get(IUserService)

Dependency scopes
-----------------
Each dependency has a :class:`DependencyScope` deciding its lifetime and the
moment its ``yield`` teardown runs:

* ``CONTAINER`` (default) — one instance per container, torn down at
  :meth:`FastAPIContainer.aclose`.
* ``SCOPED`` — one instance per active scope, torn down when that scope closes.
  A scope is opened explicitly with ``async with container.scope()`` (and
  implicitly around :meth:`FastAPIContainer.invoke`).

The scope is configurable globally (``default_scope``) and per dependency
(``scopes``). Orthogonally, FastAPI's ``use_cache`` controls whether an instance
is shared between consumers within a scope (``True``, the default) or created
fresh at each injection point (``False``).
"""

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from http.cookies import SimpleCookie
from types import MappingProxyType
from typing import Any, TypeVar, cast, overload
from urllib.parse import urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, Response
from fastapi.concurrency import contextmanager_in_threadpool, run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.security import SecurityScopes
from starlette.datastructures import State
from starlette.requests import Request

from fastapi_standalone_di._compat import (
    is_async_gen_callable,
    is_coroutine_callable,
    is_gen_callable,
)
from fastapi_standalone_di.app_state import AppState, get_app_state
from fastapi_standalone_di.registration import RegistrableDependency

T = TypeVar("T")


class _StubApp:
    """Minimal stand-in for ``request.app`` when no real application is set.

    Exposes only ``state`` (backed by the container's :class:`AppState`); any
    other attribute access is intentionally unsupported outside ASGI.
    """

    __slots__ = ("state",)

    def __init__(self, state: State) -> None:
        self.state = state


async def _empty_receive() -> dict[str, Any]:
    """ASGI receive channel for a standalone request: an empty, complete body."""
    return {"type": "http.request", "body": b"", "more_body": False}


def _build_stub_request(
    *,
    app: Any | None,
    state: State,
    query: Mapping[str, str],
    path: Mapping[str, str],
    cookies: Mapping[str, str],
) -> Request:
    """Build a fresh, self-contained ``Request`` for one resolution operation.

    The scope is complete enough that ``request.app``/``.state``,
    ``.query_params``, ``.path_params``, ``.cookies``, ``.client`` and
    ``await request.body()`` all work; query/path/cookie values mirror the
    container's per-source configuration.

    Cookie values are serialised with :class:`http.cookies.SimpleCookie`, so
    special characters (``;``, ``=``, spaces) are escaped and round-trip through
    ``request.cookies``. HTTP headers are latin-1 only, so a cookie value that is
    not latin-1 encodable (e.g. an emoji) raises :class:`UnicodeEncodeError`.
    """
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        jar: SimpleCookie = SimpleCookie()
        for name, value in cookies.items():
            jar[name] = value
        cookie_header = "; ".join(morsel.OutputString() for morsel in jar.values())
        headers.append((b"cookie", cookie_header.encode("latin-1")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": urlencode(list(query.items())).encode("latin-1"),
        "headers": headers,
        "path_params": dict(path),
        "client": None,
        "server": ("standalone", 0),
        "app": app if app is not None else _StubApp(state),
    }
    return Request(scope, receive=_empty_receive)


class _LazyRequest:
    """Memoised stub-``Request`` factory shared across one resolution operation.

    The stub is built on the first :meth:`get` — i.e. the first dependency that
    declares a ``Request``/``HTTPConnection`` param — and reused for the rest of
    the operation. A tree with no such param never builds one.
    """

    __slots__ = ("_build", "_request")

    def __init__(self, build: Callable[[], Request]) -> None:
        self._build = build
        self._request: Request | None = None

    def get(self) -> Request:
        if self._request is None:
            self._request = self._build()
        return self._request


DependencyOverrides = dict[Callable[..., Any], Callable[..., Any]]


class DependencyScope(Enum):
    """Lifetime and teardown boundary of a resolved dependency."""

    CONTAINER = "container"
    """One instance per container, torn down at :meth:`FastAPIContainer.aclose`."""

    SCOPED = "scoped"
    """One instance per active scope, torn down when that scope closes."""


# Global default: a single scope, or a mapping from FastAPI's ``Depends(scope=)``
# literals ("request"/"function") — plus ``None`` for dependencies without an
# explicit FastAPI scope — to a :class:`DependencyScope`.
DefaultScope = DependencyScope | dict[str | None, DependencyScope]
Scopes = dict[Callable[..., Any], DependencyScope]


class ScopeError(RuntimeError):
    """Raised on a scope misuse (e.g. resolving a SCOPED dependency without a scope)."""


# Sentinels a "required" (no usable default) field carries as ``field_info.default``,
# collected across the supported pydantic range (v2 ``PydanticUndefined``, v1
# ``Undefined``, plus the bare ``...`` some code paths use).
_REQUIRED_SENTINELS: tuple[Any, ...] = (Ellipsis,)
try:
    from pydantic_core import PydanticUndefined as _PydanticUndefinedV2

    _REQUIRED_SENTINELS += (_PydanticUndefinedV2,)
except ImportError:  # pragma: no cover - pydantic v1
    pass
try:
    from pydantic.fields import Undefined as _UndefinedV1  # type: ignore[attr-defined]

    _REQUIRED_SENTINELS += (_UndefinedV1,)  # pragma: no cover - pydantic v1
except ImportError:  # pragma: no cover - pydantic v2
    pass


@dataclass(frozen=True)
class ParamSource:
    """How to supply one class of connection parameters (query/path/header/cookie).

    Outside ASGI these values don't arrive over the wire, so the container has to
    produce them. Both channels carry **strings**, exactly as HTTP would, and are
    coerced to each parameter's declared type by FastAPI's own field validation.

    Attributes
    ----------
    values:
        Explicit values keyed by parameter name (falling back to its alias).
    default:
        A string injected for any **required** parameter of this source that has
        no explicit value and no declared default. Left as ``None``, such a
        parameter raises :class:`MissingParameterError` instead.
    """

    values: Mapping[str, str] = field(default_factory=dict)
    default: str | None = None


# A source argument accepts either a bare ``{name: value}`` mapping (values only)
# or a full :class:`ParamSource`.
ParamSourceArg = ParamSource | Mapping[str, str]


class ParameterError(RuntimeError):
    """Base class for standalone connection-parameter resolution errors."""


class MissingParameterError(ParameterError):
    """A required query/path/header/cookie parameter could not be supplied."""

    def __init__(self, source: str, name: str, call: Callable[..., Any]) -> None:
        self.source = source
        self.name = name
        target = getattr(call, "__qualname__", repr(call))
        super().__init__(
            f"Required {source} parameter {name!r} of {target} has no value in a "
            f"standalone context. Provide it via {source}={{{name!r}: ...}}, set a "
            f"{source} default, or make the parameter optional."
        )


class ParameterValidationError(ParameterError):
    """A supplied parameter value is incompatible with its declared type."""

    def __init__(
        self,
        source: str,
        name: str,
        value: object,
        errors: object,
        call: Callable[..., Any],
    ) -> None:
        self.source = source
        self.name = name
        self.errors = errors
        target = getattr(call, "__qualname__", repr(call))
        super().__init__(
            f"{source.capitalize()} parameter {name!r} of {target} got an invalid "
            f"value {value!r}: {errors}"
        )


def _as_param_source(arg: ParamSourceArg | None) -> ParamSource:
    """Normalise a source argument to a :class:`ParamSource`."""
    if arg is None:
        return ParamSource()
    if isinstance(arg, ParamSource):
        return arg
    return ParamSource(values=arg)


class DependantCache:
    """Cache of FastAPI dependency-tree introspection results.

    Maps a dependency callable to the ``Dependant`` produced by FastAPI's
    ``get_dependant``, so repeated :meth:`FastAPIContainer.resolve` calls skip
    re-introspecting the same callables. A single instance can be shared across
    several containers.
    """

    __slots__ = ("dependants",)

    def __init__(self) -> None:
        self.dependants: dict[Callable[..., Any], Dependant] = {}

    def get_dependant(self, call: Callable[..., Any]) -> Dependant | None:
        """Look up a cached ``Dependant`` by callable."""
        return self.dependants.get(call)

    def set_dependant(self, call: Callable[..., Any], dependant: Dependant) -> None:
        """Store a ``Dependant`` keyed by callable."""
        self.dependants[call] = dependant

    def clear(self) -> None:
        """Drop all cached entries."""
        self.dependants.clear()


class ResolvedDependencies:
    """Container for dependencies resolved by :meth:`FastAPIContainer.resolve`.

    :meth:`get` / :meth:`optional` address only the **top-level** dependencies
    explicitly passed to :meth:`FastAPIContainer.resolve` (or the entry point of
    :meth:`FastAPIContainer.invoke_resolved`). Pass ``transitive=True`` to widen
    the lookup to the sub-dependencies resolved along the way, or iterate over
    the complete set with :meth:`all_instances`; everything is keyed by its
    resolved callable.

    A dependency injected with FastAPI's ``use_cache=False`` is rebuilt fresh at
    each injection point; only the last instance built during the operation is
    retained under its callable key, so such duplicates are not individually
    addressable here.
    """

    __slots__ = ("_all", "_instances")

    def __init__(
        self,
        instances: dict[Callable[..., Any], Any],
        all_instances: dict[Callable[..., Any], Any] | None = None,
    ) -> None:
        self._instances = instances
        self._all = all_instances if all_instances is not None else instances

    @overload
    def get(self, dependency: type[T], *, transitive: bool = False) -> T: ...

    @overload
    def get(self, dependency: Callable[..., T], *, transitive: bool = False) -> T: ...

    def get(self, dependency: Callable[..., Any], *, transitive: bool = False) -> Any:
        """Retrieve a resolved dependency by its type or callable.

        By default only the **top-level** dependencies (those explicitly passed
        to :meth:`FastAPIContainer.resolve`) are addressable. Pass
        ``transitive=True`` to also reach a sub-dependency resolved along the
        way. Raises :class:`KeyError` if the dependency was not resolved.
        """
        key = _resolve_callable(dependency)
        source = self._all if transitive else self._instances
        try:
            return source[key]
        except KeyError:
            name = getattr(dependency, "__qualname__", repr(dependency))
            module = getattr(dependency, "__module__", "?")
            if transitive:
                raise KeyError(
                    f"Dependency {module}.{name} was not resolved as part of "
                    "this operation."
                ) from None
            if key in self._all:
                raise KeyError(
                    f"Dependency {module}.{name} was resolved as a sub-dependency, "
                    "not a top-level one. Pass transitive=True to retrieve it."
                ) from None
            raise KeyError(
                f"Dependency {module}.{name} was not resolved. "
                "Did you pass it to resolve()?"
            ) from None

    @overload
    def optional(
        self, dependency: type[T], *, transitive: bool = False
    ) -> T | None: ...

    @overload
    def optional(
        self, dependency: Callable[..., T], *, transitive: bool = False
    ) -> T | None: ...

    def optional(
        self, dependency: Callable[..., Any], *, transitive: bool = False
    ) -> Any | None:
        """Retrieve a resolved dependency, or ``None`` if not resolved.

        As with :meth:`get`, ``transitive=True`` widens the lookup to
        sub-dependencies resolved along the way.
        """
        key = _resolve_callable(dependency)
        source = self._all if transitive else self._instances
        return source.get(key)

    def all_instances(self) -> Mapping[Callable[..., Any], Any]:
        """Return a read-only view of every instance resolved in this operation.

        Keyed by resolved callable and ordered by resolution (sub-dependencies
        before the dependents that consume them). Includes both the top-level
        dependencies and their sub-dependencies.
        """
        return MappingProxyType(self._all)


def _resolve_callable(dep: Callable[..., Any]) -> Callable[..., Any]:
    """If *dep* is a ``RegistrableDependency``, return its registered impl."""
    if inspect.isclass(dep) and issubclass(dep, RegistrableDependency):
        return dep.dependency()
    return dep


# --- public API -----------------------------------------------------------


class FastAPIContainer:
    """Dependency container that resolves FastAPI dependencies outside ASGI.

    Encapsulates the configuration needed to resolve a dependency tree:
    application state, dependency overrides, introspection cache, the
    :class:`DependencyScope` policy, the query/path/header/cookie parameter
    values to supply outside ASGI (see :class:`ParamSource`), and the security
    scopes to expose to any ``SecurityScopes`` parameter.

    Example::

        container = FastAPIContainer(
            app_state=AppState.from_app(app),
            dependency_overrides={get_db: lambda: mock_db},
            default_scope=DependencyScope.CONTAINER,
            scopes={get_db_session: DependencyScope.SCOPED},
        )
        service = await container.get(IMyService)

        async with container.scope() as scope:
            session = await scope.get(get_db_session)
        # SCOPED dependencies torn down here; CONTAINER ones survive until aclose()
    """

    def __init__(
        self,
        app_state: AppState | None = None,
        dependency_overrides: DependencyOverrides | None = None,
        dependant_cache: DependantCache | bool = True,
        default_scope: DefaultScope = DependencyScope.CONTAINER,
        scopes: Scopes | None = None,
        app: Any | None = None,
        query: ParamSourceArg | None = None,
        path: ParamSourceArg | None = None,
        headers: ParamSourceArg | None = None,
        cookies: ParamSourceArg | None = None,
        security_scopes: Sequence[str] | None = None,
    ) -> None:
        self._app_state = app_state if app_state is not None else AppState.standalone()
        self._dependency_overrides = dependency_overrides or {}
        self._app = app
        self._query = _as_param_source(query)
        self._path = _as_param_source(path)
        self._headers = _as_param_source(headers)
        self._cookies = _as_param_source(cookies)
        self._security_scopes: list[str] = (
            list(security_scopes) if security_scopes else []
        )

        dc: DependantCache | None
        if isinstance(dependant_cache, DependantCache):
            dc = dependant_cache
        elif dependant_cache:
            dc = DependantCache()
        else:
            dc = None
        self._dependant_cache = dc

        self._default_scope = default_scope
        self._scopes = scopes or {}

        self._container_instances: dict[Callable[..., Any], Any] = {}
        self._seed_container_instances()
        self._container_stack = AsyncExitStack()
        self._container_locks: dict[Callable[..., Any], asyncio.Lock] = {}

    def _seed_container_instances(self) -> None:
        """Pre-populate the ``get_app_state`` and ``get_container`` instances.

        Seeding ``get_app_state`` lets it resolve without a live request, and
        seeding ``get_container`` makes ``Depends(get_container)`` yield the
        resolving container itself (used by lazy ``singleton``s) without the
        container having to register itself in its own ``AppState``. An override
        for either takes precedence: leaving the key unseeded lets
        :meth:`_resolve_single` route through :meth:`_apply_overrides`.
        """
        if get_app_state not in self._dependency_overrides:
            self._container_instances[get_app_state] = self._app_state
        if get_container not in self._dependency_overrides:
            self._container_instances[get_container] = self
        if _get_container_optional not in self._dependency_overrides:
            self._container_instances[_get_container_optional] = self

    def clear_cache(self) -> None:
        """Drop all cached container-scoped instances.

        The ``get_app_state`` seed is restored (unless overridden) so
        subsequent :meth:`resolve` calls still work. Does not run teardown —
        closing generator dependencies still happens at :meth:`aclose`.
        """
        self._container_instances.clear()
        self._container_locks.clear()
        self._seed_container_instances()

    def scope(self) -> "ResolutionScope":
        """Open a resolution scope for ``SCOPED`` dependencies.

        Use as an async context manager; ``SCOPED`` dependencies resolved
        through the returned object are torn down when the ``async with`` block
        exits, while ``CONTAINER`` dependencies remain owned by the container.
        """
        return ResolutionScope(self)

    @overload
    async def get(self, dependency: type[T]) -> T: ...

    @overload
    async def get(self, dependency: Callable[..., T]) -> T: ...

    async def get(self, dependency: Callable[..., Any]) -> Any:
        """Resolve a single dependency and return its instance directly."""
        deps = await self.resolve(dependency)
        return deps.get(dependency)

    @overload
    async def optional(self, dependency: type[T]) -> T | None: ...

    @overload
    async def optional(self, dependency: Callable[..., T]) -> T | None: ...

    async def optional(self, dependency: Callable[..., Any]) -> Any | None:
        """Resolve a single dependency, returning ``None`` if not resolved."""
        deps = await self.resolve(dependency)
        return deps.optional(dependency)

    async def invoke(self, call: Callable[..., Any]) -> Any:
        """Resolve all ``Depends()`` parameters of *call* and invoke it.

        Runs inside an implicit resolution scope: ``SCOPED`` dependencies used
        by *call* are torn down once *call* returns. Unlike :meth:`resolve`,
        the result is not cached — the callable is treated as an entry point.
        """
        async with self.scope() as scope:
            return await scope.invoke(call)

    async def invoke_resolved(self, call: Callable[..., Any]) -> ResolvedDependencies:
        """Resolve and invoke *call*, returning the resolved dependencies.

        Like :meth:`invoke`, *call* runs inside an implicit resolution scope, so
        ``SCOPED`` dependencies are torn down before this returns: the returned
        bag still references those instances for inspection, but their ``yield``
        teardown has already run. ``CONTAINER`` instances remain live on the
        container. The bag's :meth:`~ResolvedDependencies.get` for *call* is the
        invocation result.
        """
        async with self.scope() as scope:
            return await scope.invoke_resolved(call)

    async def resolve(
        self,
        *dependencies: Callable[..., Any],
    ) -> ResolvedDependencies:
        """Resolve one or more FastAPI dependencies at container scope.

        Resolved ``CONTAINER`` instances are cached on the container: subsequent
        calls reuse them. Resolving a ``SCOPED`` dependency here raises
        :class:`ScopeError` — open a :meth:`scope` (or use :meth:`invoke`) for
        those.

        Parameters
        ----------
        *dependencies:
            The dependency callables (classes or functions) to resolve.
            ``RegistrableDependency`` interfaces are automatically dereferenced
            to their registered implementations.

        Returns
        -------
        ResolvedDependencies
            A container holding all resolved instances.
        """
        return await self._resolve_many(dependencies, active_scope=None)

    async def aclose(self) -> None:
        """Close the container, running teardown for CONTAINER ``yield`` deps."""
        await self._container_stack.aclose()

    async def __aenter__(self) -> "FastAPIContainer":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- internals ---------------------------------------------------------

    def _apply_overrides(self, call: Callable[..., Any]) -> Callable[..., Any]:
        """Apply dependency overrides, returning the substitute if one exists."""
        return self._dependency_overrides.get(call, call)

    def _build_request(self) -> Request:
        """Build the stub ``Request`` shared across one resolution operation."""
        return _build_stub_request(
            app=self._app,
            state=self._app_state.as_state(),
            query=self._query.values,
            path=self._path.values,
            cookies=self._cookies.values,
        )

    def _resolve_param(
        self,
        source: str,
        param_field: Any,
        config: ParamSource,
        call: Callable[..., Any],
    ) -> Any:
        """Produce a value for one query/path/header/cookie parameter.

        Precedence: an explicit value (by name, then alias) → the parameter's own
        declared default → the source-wide ``default`` string → a
        :class:`MissingParameterError` for a required parameter left unsupplied.
        Supplied strings are coerced to the declared type by FastAPI's field
        validation, so an incompatible value raises
        :class:`ParameterValidationError` rather than reaching the callable.
        """
        for key in (param_field.name, param_field.alias):
            if key in config.values:
                return self._coerce_param(source, param_field, config.values[key], call)

        field_info = param_field.field_info
        if getattr(field_info, "default_factory", None) is not None:
            return field_info.default_factory()
        if not any(field_info.default is sentinel for sentinel in _REQUIRED_SENTINELS):
            return field_info.default

        if config.default is not None:
            return self._coerce_param(source, param_field, config.default, call)

        raise MissingParameterError(source, param_field.name, call)

    @staticmethod
    def _coerce_param(
        source: str,
        param_field: Any,
        raw: str,
        call: Callable[..., Any],
    ) -> Any:
        """Coerce a raw string to the parameter's declared type via FastAPI."""
        value, errors = param_field.validate(raw, {}, loc=(source, param_field.alias))
        if errors:
            raise ParameterValidationError(source, param_field.name, raw, errors, call)
        return value

    def _scope_of(
        self,
        original: Callable[..., Any],
        resolved: Callable[..., Any],
        fastapi_scope: str | None,
    ) -> DependencyScope:
        """Determine a dependency's scope.

        Precedence: the ``scopes`` map (keyed by either the declared callable or
        its resolved implementation) wins; then, if ``default_scope`` is a dict,
        the FastAPI ``Depends(scope=)`` value is mapped through it; otherwise the
        single ``default_scope`` applies. ``get_app_state`` is always CONTAINER.
        """
        if original is get_app_state or resolved is get_app_state:
            return DependencyScope.CONTAINER
        for key in (original, resolved):
            override = self._scopes.get(key)
            if override is not None:
                return override
        default = self._default_scope
        if isinstance(default, dict):
            if fastapi_scope in default:
                return default[fastapi_scope]
            return default.get(None, DependencyScope.CONTAINER)
        return default

    def _target(
        self,
        active_scope: "ResolutionScope | None",
        dep_scope: DependencyScope,
        call: Callable[..., Any],
    ) -> tuple[
        dict[Callable[..., Any], Any],
        AsyncExitStack,
        dict[Callable[..., Any], asyncio.Lock],
    ]:
        """Return the (instance cache, exit stack, locks) a *dep_scope* dep uses."""
        if dep_scope is DependencyScope.CONTAINER:
            return (
                self._container_instances,
                self._container_stack,
                self._container_locks,
            )
        if active_scope is None:
            name = getattr(call, "__qualname__", repr(call))
            raise ScopeError(
                f"{name} is SCOPED but was resolved without an active scope. "
                "Open one with `async with container.scope() as scope: "
                "await scope.get(...)`, or use `container.invoke(...)`."
            )
        return (
            active_scope._scope_instances,
            active_scope._scope_stack,
            active_scope._scope_locks,
        )

    async def _resolve_many(
        self,
        dependencies: tuple[Callable[..., Any], ...],
        *,
        active_scope: "ResolutionScope | None",
    ) -> ResolvedDependencies:
        instances: dict[Callable[..., Any], Any] = {}
        collected: dict[Callable[..., Any], Any] = {}
        request = _LazyRequest(self._build_request)
        for dep in dependencies:
            resolved = _resolve_callable(dep)
            dep_scope = self._scope_of(dep, resolved, None)
            instances[resolved] = await self._resolve_single(
                resolved,
                active_scope=active_scope,
                dep_scope=dep_scope,
                request=request,
                collected=collected,
            )
        return ResolvedDependencies(instances, collected)

    def _collect_cached_subtree(
        self,
        call: Callable[..., Any],
        *,
        active_scope: "ResolutionScope | None",
        collected: dict[Callable[..., Any], Any],
    ) -> None:
        """Record the already-cached sub-dependencies of a cache-hit *call*.

        On a cache hit, :meth:`_resolve_single` skips :meth:`_instantiate`, so
        the sub-dependencies are never walked and would be missing from
        *collected*. This mirrors that walk over the cached ``Dependant`` and
        pulls each sub-dependency straight from its scope cache — it never
        rebuilds an instance nor mutates a cache, so resolution and caching
        semantics are untouched. Sub-dependencies absent from any cache (e.g.
        ``use_cache=False`` transients) are simply skipped; each is recorded
        after its own descendants so the resolution order still holds.
        """
        effective_call = self._apply_overrides(call)
        dependant = _get_dependant(effective_call, self._dependant_cache)
        for sub_dep in dependant.dependencies:
            original = cast("Callable[..., Any]", sub_dep.call)
            sub_call = _resolve_callable(original)
            if sub_call in collected:
                continue
            sub_scope = self._scope_of(
                original, sub_call, getattr(sub_dep, "scope", None)
            )
            instances, _, _ = self._target(active_scope, sub_scope, sub_call)
            if sub_call not in instances:
                continue
            self._collect_cached_subtree(
                sub_call, active_scope=active_scope, collected=collected
            )
            collected[sub_call] = instances[sub_call]

    async def _resolve_single(
        self,
        call: Callable[..., Any],
        *,
        active_scope: "ResolutionScope | None",
        dep_scope: DependencyScope,
        cache: bool = True,
        use_cache: bool = True,
        request: _LazyRequest | None = None,
        collected: dict[Callable[..., Any], Any] | None = None,
    ) -> Any:
        """Recursively resolve a single dependency and all its sub-dependencies.

        *dep_scope* is the resolved scope of *call*; *cache* controls whether
        *call* itself is cached (``invoke`` passes ``False`` for the entry
        point); *use_cache* mirrors FastAPI's per-dependency caching flag.
        *request* is the memoised stub-connection factory shared across this
        resolution operation; the top-level call creates it when none is
        threaded in, and no stub is built unless a dependency needs one.
        *collected*, when provided, records every instance resolved during the
        operation (keyed by callable) so :class:`ResolvedDependencies` can
        expose sub-dependencies; it never influences resolution or caching.
        """
        if request is None:
            request = _LazyRequest(self._build_request)
        instances, exit_stack, locks = self._target(active_scope, dep_scope, call)

        shared = cache and use_cache
        if shared and call in instances:
            instance = instances[call]
            if collected is not None:
                self._collect_cached_subtree(
                    call, active_scope=active_scope, collected=collected
                )
        elif not shared:
            instance = await self._instantiate(
                call,
                active_scope=active_scope,
                dep_scope=dep_scope,
                cache=cache,
                use_cache=use_cache,
                request=request,
                exit_stack=exit_stack,
                collected=collected,
            )
        else:
            # Serialise concurrent resolutions of the same shared dependency:
            # without this, two ``get``/``invoke`` racing on a cache miss would
            # both build an instance and both enter the shared exit stack,
            # leaking a duplicate and its teardown. Double-check the cache after
            # acquiring the lock.
            lock = locks.setdefault(call, asyncio.Lock())
            async with lock:
                if call in instances:
                    instance = instances[call]
                    if collected is not None:
                        self._collect_cached_subtree(
                            call, active_scope=active_scope, collected=collected
                        )
                else:
                    instance = await self._instantiate(
                        call,
                        active_scope=active_scope,
                        dep_scope=dep_scope,
                        cache=cache,
                        use_cache=use_cache,
                        request=request,
                        exit_stack=exit_stack,
                        collected=collected,
                    )
                    instances[call] = instance

        if collected is not None:
            collected[call] = instance
        return instance

    async def _instantiate(
        self,
        call: Callable[..., Any],
        *,
        active_scope: "ResolutionScope | None",
        dep_scope: DependencyScope,
        cache: bool,
        use_cache: bool,
        request: _LazyRequest,
        exit_stack: AsyncExitStack,
        collected: dict[Callable[..., Any], Any] | None = None,
    ) -> Any:
        """Build *call*'s instance, resolving sub-dependencies and injecting stub
        request/response/param values; teardown is registered on *exit_stack*.

        Always constructs a fresh instance — caching is the caller's concern.
        *collected* is threaded into sub-dependency resolution so they too are
        recorded for :class:`ResolvedDependencies`.
        """
        # Apply overrides: if the original callable has an override, use it instead.
        effective_call = self._apply_overrides(call)

        dependant = _get_dependant(effective_call, self._dependant_cache)

        # Resolve sub-dependencies first.
        sub_values: dict[str, Any] = {}
        for sub_dep in dependant.dependencies:
            original = cast("Callable[..., Any]", sub_dep.call)
            # Resolve the RegistrableDependency indirection that Depends may apply
            # at the Dependant level.
            sub_call = _resolve_callable(original)
            sub_scope = self._scope_of(
                original, sub_call, getattr(sub_dep, "scope", None)
            )
            if (
                dep_scope is DependencyScope.CONTAINER
                and cache
                and use_cache
                and sub_scope is DependencyScope.SCOPED
            ):
                parent = getattr(call, "__qualname__", repr(call))
                child = getattr(sub_call, "__qualname__", repr(sub_call))
                raise ScopeError(
                    f"CONTAINER-scoped {parent} cannot depend on SCOPED {child}: "
                    "the container would capture a dependency torn down at scope "
                    "close (captive dependency). Make the dependent SCOPED too."
                )
            sub_instance = await self._resolve_single(
                sub_call,
                active_scope=active_scope,
                dep_scope=sub_scope,
                use_cache=getattr(sub_dep, "use_cache", True),
                request=request,
                collected=collected,
            )
            if sub_dep.name is not None:
                sub_values[sub_dep.name] = sub_instance

        # Outside ASGI there is no live connection, so inject the operation's
        # stub Request for any Request/HTTPConnection/WebSocket parameter.
        #
        # Identify connection parameters by the names FastAPI resolved from the
        # typed hints (stable across the supported range), not by re-inspecting
        # annotations: under ``from __future__ import annotations`` the latter are
        # plain strings and a runtime type check would silently miss them.
        sig_params = inspect.signature(effective_call).parameters
        for conn_param in (
            getattr(dependant, "request_param_name", None),
            getattr(dependant, "http_connection_param_name", None),
            getattr(dependant, "websocket_param_name", None),
        ):
            if conn_param is None or conn_param in sub_values:
                continue
            # Preserve the optional-connection pattern (e.g. ``request: Request =
            # None``, as used by get_app_state): leave it to its default.
            param = sig_params.get(conn_param)
            if param is not None and param.default is None:
                continue
            sub_values[conn_param] = request.get()

        # A dependency may declare ``response: Response`` (to set headers/cookies/
        # status) or ``background_tasks: BackgroundTasks``. FastAPI records the
        # parameter names on the ``Dependant``. Standalone there is no transport,
        # so inject stubs: a fresh ``Response`` whose mutations are accepted but
        # have no effect (nothing sends it), and a ``BackgroundTasks`` whose
        # collected tasks run when the owning scope closes — registered on that
        # scope's exit stack (the container's for CONTAINER, the resolution
        # scope's for SCOPED).
        response_param_name = getattr(dependant, "response_param_name", None)
        if response_param_name is not None and response_param_name not in sub_values:
            sub_values[response_param_name] = Response()

        bg_param_name = getattr(dependant, "background_tasks_param_name", None)
        if bg_param_name is not None and bg_param_name not in sub_values:
            background_tasks = BackgroundTasks()
            sub_values[bg_param_name] = background_tasks
            exit_stack.push_async_callback(background_tasks)

        # A dependency (typically a security dependency) may declare
        # ``scopes: SecurityScopes``. In ASGI FastAPI fills it from the OAuth2
        # scopes accumulated along the dependency chain; standalone there is no
        # such chain, so inject a ``SecurityScopes`` carrying the container's
        # configured scopes — supplied like query/header/cookie values rather
        # than derived from a request (empty by default).
        security_scopes_param_name = getattr(
            dependant, "security_scopes_param_name", None
        )
        if (
            security_scopes_param_name is not None
            and security_scopes_param_name not in sub_values
        ):
            sub_values[security_scopes_param_name] = SecurityScopes(
                scopes=list(self._security_scopes)
            )

        for source_name, param_fields, config in (
            ("header", dependant.header_params, self._headers),
            ("query", dependant.query_params, self._query),
            ("path", dependant.path_params, self._path),
            ("cookie", dependant.cookie_params, self._cookies),
        ):
            for param_field in param_fields:
                if param_field.name in sub_values:
                    continue
                sub_values[param_field.name] = self._resolve_param(
                    source_name, param_field, config, effective_call
                )

        # Determine the callable's execution model from the call itself rather
        # than from ``Dependant`` attributes: the ``is_*_callable`` flags only
        # became attributes of ``Dependant`` in recent FastAPI, whereas these
        # module-level helpers have been stable across the supported range.
        async_gen = is_async_gen_callable(effective_call)
        sync_gen = is_gen_callable(effective_call)

        # Invoke the callable itself. Generators are entered on the scope's exit
        # stack so their teardown runs when that scope closes (the container for
        # CONTAINER, the resolution scope for SCOPED).
        if async_gen:
            cm = asynccontextmanager(effective_call)(**sub_values)
            instance = await exit_stack.enter_async_context(cm)
        elif sync_gen:
            cm = contextmanager_in_threadpool(
                contextmanager(effective_call)(**sub_values)
            )
            instance = await exit_stack.enter_async_context(cm)
        elif is_coroutine_callable(effective_call):
            instance = await effective_call(**sub_values)
        else:
            instance = await run_in_threadpool(effective_call, **sub_values)

        return instance


class ResolutionScope:
    """A short-lived resolution scope owning ``SCOPED`` dependency lifetimes.

    Obtained from :meth:`FastAPIContainer.scope` and used as an async context
    manager. ``SCOPED`` dependencies resolved through it are torn down when the
    block exits; ``CONTAINER`` dependencies are delegated to the parent
    container and outlive the scope.
    """

    __slots__ = ("_container", "_scope_instances", "_scope_locks", "_scope_stack")

    def __init__(self, container: FastAPIContainer) -> None:
        self._container = container
        self._scope_instances: dict[Callable[..., Any], Any] = {}
        self._scope_stack = AsyncExitStack()
        self._scope_locks: dict[Callable[..., Any], asyncio.Lock] = {}

    async def __aenter__(self) -> "ResolutionScope":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._scope_stack.aclose()

    @overload
    async def get(self, dependency: type[T]) -> T: ...

    @overload
    async def get(self, dependency: Callable[..., T]) -> T: ...

    async def get(self, dependency: Callable[..., Any]) -> Any:
        """Resolve a single dependency within this scope."""
        deps = await self.resolve(dependency)
        return deps.get(dependency)

    @overload
    async def optional(self, dependency: type[T]) -> T | None: ...

    @overload
    async def optional(self, dependency: Callable[..., T]) -> T | None: ...

    async def optional(self, dependency: Callable[..., Any]) -> Any | None:
        """Resolve a single dependency within this scope, or ``None``."""
        deps = await self.resolve(dependency)
        return deps.optional(dependency)

    async def resolve(
        self,
        *dependencies: Callable[..., Any],
    ) -> ResolvedDependencies:
        """Resolve one or more dependencies within this scope."""
        return await self._container._resolve_many(dependencies, active_scope=self)

    async def invoke(self, call: Callable[..., Any]) -> Any:
        """Resolve *call*'s dependencies within this scope and invoke it."""
        return (await self.invoke_resolved(call)).get(call)

    async def invoke_resolved(self, call: Callable[..., Any]) -> ResolvedDependencies:
        """Resolve and invoke *call*, returning the resolved dependencies.

        The returned bag's :meth:`~ResolvedDependencies.get` for *call* yields
        the invocation result, while ``get(dep, transitive=True)`` and
        :meth:`~ResolvedDependencies.all_instances` expose every sub-dependency
        resolved for the call.
        """
        collected: dict[Callable[..., Any], Any] = {}
        dep_scope = self._container._scope_of(call, call, None)
        instance = await self._container._resolve_single(
            call,
            active_scope=self,
            dep_scope=dep_scope,
            cache=False,
            collected=collected,
        )
        return ResolvedDependencies({_resolve_callable(call): instance}, collected)


def get_container(
    app_state: AppState = Depends(get_app_state),
) -> FastAPIContainer:
    """FastAPI dependency returning the active :class:`FastAPIContainer`.

    Register the container in ``app_state`` at startup, e.g. via
    ``set_app_state_value("container", FastAPIContainer(...))``.
    """
    container: FastAPIContainer | None = app_state.get("container")
    if container is None:
        raise RuntimeError(
            "No FastAPIContainer registered in app_state — "
            'register one via set_app_state_value("container", FastAPIContainer(...)).'
        )
    return container


def _get_container_optional(
    app_state: AppState = Depends(get_app_state),
) -> "FastAPIContainer | None":
    """Like :func:`get_container` but returns ``None`` instead of raising.

    Lazy ``singleton`` wrappers depend on this rather than on :func:`get_container`:
    FastAPI resolves a dependency's whole ``Depends(...)`` tree before the wrapper
    body runs, so depending on the raising :func:`get_container` would fail even
    when a preset value makes the container unnecessary. Resolving *this* never
    fails; the wrapper itself raises only on a genuine cache miss with no
    container available. Under :class:`FastAPIContainer` it is seeded to the
    resolving container, exactly like :func:`get_container`.
    """
    container: FastAPIContainer | None = app_state.get("container")
    return container


@asynccontextmanager
async def container_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """ASGI lifespan that installs a :class:`FastAPIContainer` and closes it.

    Wires a container backed by the application state into ``app.state.container``
    so ``get_container`` and lazy :func:`~fastapi_standalone_di.singleton.singleton`
    dependencies resolve during requests, and — crucially — owns its teardown:
    the container's ``CONTAINER``-scoped ``yield`` dependencies are closed at
    application shutdown via :meth:`FastAPIContainer.aclose`.

    Usage::

        app = FastAPI(lifespan=container_lifespan)

    Compose it with your own startup/shutdown from a wrapping lifespan::

        @asynccontextmanager
        async def lifespan(app):
            async with container_lifespan(app):
                ...  # your own startup
                yield

    The container is reachable via ``app.state.container`` throughout; nothing is
    yielded into the lifespan state, so it stays compatible across the whole
    supported Starlette range. Requires a FastAPI version that honours the
    ``lifespan`` argument (FastAPI >= 0.93); on older releases register the
    container manually at startup instead.
    """
    container = FastAPIContainer(app_state=AppState.from_app(app))
    app.state.container = container
    try:
        yield
    finally:
        await container.aclose()


def _get_dependant(
    call: Callable[..., Any],
    dependant_cache: DependantCache | None,
) -> Dependant:
    """Retrieve or compute the ``Dependant`` for *call*, using the cache if provided."""
    if dependant_cache is not None:
        cached = dependant_cache.get_dependant(call)
        if cached is not None:
            return cached

    dependant = get_dependant(path="", call=call)

    if dependant_cache is not None:
        dependant_cache.set_dependant(call, dependant)

    return dependant
