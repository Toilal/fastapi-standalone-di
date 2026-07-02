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
"""

import inspect
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from typing import Any, cast, overload

from fastapi import Depends
from fastapi.concurrency import contextmanager_in_threadpool, run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from starlette.requests import HTTPConnection, Request

from fastapi_standalone_di._compat import (
    is_async_gen_callable,
    is_coroutine_callable,
    is_gen_callable,
)
from fastapi_standalone_di.app_state import AppState, get_app_state
from fastapi_standalone_di.registration import RegistrableDependency

# Stub Request used when resolving dependencies outside ASGI.
_STUB_REQUEST = Request(
    scope={
        "type": "http",
        "method": "GET",
        "headers": [],
        "query_string": b"",
        "path": "/",
        "root_path": "",
        "path_params": {},
    }
)

DependencyOverrides = dict[Callable[..., Any], Callable[..., Any]]


class DependantCache:
    """Cache of FastAPI dependency-tree introspection results.

    Maps a dependency callable to the ``Dependant`` produced by FastAPI's
    ``get_dependant``, so repeated :meth:`FastAPIContainer.resolve` calls skip
    re-introspecting the same callables. A single instance can be shared across
    several containers.
    """

    __slots__ = ("_keepalive", "dependants")

    def __init__(self) -> None:
        # Keyed by ``id(call)``.
        self.dependants: dict[int, Dependant] = {}
        # Strong refs to every object whose ``id()`` is used as a cache key.
        # Python recycles ``id()`` (memory addresses) after an object is GC'd,
        # so without this a short-lived callable could be collected and a later
        # function allocated at the same address, then wrongly served the dead
        # object's cached dependant. Holding a reference keeps the address
        # reserved for the cache's lifetime.
        self._keepalive: dict[int, object] = {}

    def keep_alive(self, *objs: object) -> None:
        """Pin objects so their ``id()`` stays reserved while cached."""
        for obj in objs:
            self._keepalive[id(obj)] = obj

    def get_dependant(self, call: Callable[..., Any]) -> Dependant | None:
        """Look up a cached ``Dependant`` by callable."""
        return self.dependants.get(id(call))

    def set_dependant(self, call: Callable[..., Any], dependant: Dependant) -> None:
        """Store a ``Dependant`` keyed by callable."""
        self.dependants[id(call)] = dependant
        self._keepalive[id(call)] = call

    def clear(self) -> None:
        """Drop all cached entries."""
        self.dependants.clear()
        self._keepalive.clear()


class ResolvedDependencies:
    """Container for dependencies resolved by :meth:`FastAPIContainer.resolve`."""

    __slots__ = ("_instances",)

    def __init__(
        self,
        instances: dict[Callable[..., Any], Any],
    ) -> None:
        self._instances = instances

    @overload
    def get[T](self, dependency: type[T]) -> T: ...

    @overload
    def get[T](self, dependency: Callable[..., T]) -> T: ...

    def get(self, dependency: Callable[..., Any]) -> Any:
        """Retrieve a resolved dependency by its type or callable.

        Raises :class:`KeyError` if the dependency was not resolved.
        """
        key = _resolve_callable(dependency)
        try:
            return self._instances[key]
        except KeyError:
            name = getattr(dependency, "__qualname__", repr(dependency))
            module = getattr(dependency, "__module__", "?")
            raise KeyError(
                f"Dependency {module}.{name} was not resolved. "
                "Did you pass it to resolve()?"
            ) from None

    @overload
    def optional[T](self, dependency: type[T]) -> T | None: ...

    @overload
    def optional[T](self, dependency: Callable[..., T]) -> T | None: ...

    def optional(self, dependency: Callable[..., Any]) -> Any | None:
        """Retrieve a resolved dependency, or ``None`` if not resolved."""
        key = _resolve_callable(dependency)
        return self._instances.get(key)


def _resolve_callable(dep: Callable[..., Any]) -> Callable[..., Any]:
    """If *dep* is a ``RegistrableDependency``, return its registered impl."""
    if inspect.isclass(dep) and issubclass(dep, RegistrableDependency):
        impl = dep.dependency()
        if impl is None:  # pragma: no cover
            raise RuntimeError(
                f"No implementation registered for {dep.__module__}.{dep.__qualname__}"
            )
        return impl
    return dep


# --- public API -----------------------------------------------------------


class FastAPIContainer:
    """Dependency container that resolves FastAPI dependencies outside ASGI.

    Encapsulates the configuration needed to resolve a dependency tree:
    application state, dependency overrides, and introspection cache.

    Example::

        container = FastAPIContainer(
            app_state=AppState.from_app(app),
            dependency_overrides={get_db: lambda: mock_db},
        )
        service = await container.get(IMyService)
    """

    def __init__(
        self,
        app_state: AppState | None = None,
        dependency_overrides: DependencyOverrides | None = None,
        dependant_cache: DependantCache | bool = True,
    ) -> None:
        self._app_state = app_state if app_state is not None else AppState.standalone()
        self._dependency_overrides = dependency_overrides or {}

        dc: DependantCache | None
        if isinstance(dependant_cache, DependantCache):
            dc = dependant_cache
        elif dependant_cache:
            dc = DependantCache()
        else:
            dc = None
        self._dependant_cache = dc
        self._instance_cache: dict[Callable[..., Any], Any] = {
            get_app_state: self._app_state,
        }
        self._exit_stack = AsyncExitStack()

    def clear_cache(self) -> None:
        """Drop all cached dependency instances.

        The ``get_app_state`` seed is preserved so subsequent
        :meth:`resolve` calls still work.
        """
        self._instance_cache.clear()
        self._instance_cache[get_app_state] = self._app_state

    @overload
    async def get[T](self, dependency: type[T]) -> T: ...

    @overload
    async def get[T](self, dependency: Callable[..., T]) -> T: ...

    async def get(self, dependency: Callable[..., Any]) -> Any:
        """Resolve a single dependency and return its instance directly."""
        deps = await self.resolve(dependency)
        return deps.get(dependency)

    @overload
    async def optional[T](self, dependency: type[T]) -> T | None: ...

    @overload
    async def optional[T](self, dependency: Callable[..., T]) -> T | None: ...

    async def optional(self, dependency: Callable[..., Any]) -> Any | None:
        """Resolve a single dependency, returning ``None`` if not resolved."""
        deps = await self.resolve(dependency)
        return deps.optional(dependency)

    async def invoke(self, call: Callable[..., Any]) -> Any:
        """Resolve all ``Depends()`` parameters of *call* and invoke it.

        Unlike :meth:`resolve`, this does **not** cache the result — the
        callable is treated as an entry point, not a reusable dependency.
        """
        return await self._resolve_single(call)

    async def resolve(
        self,
        *dependencies: Callable[..., Any],
    ) -> ResolvedDependencies:
        """Resolve one or more FastAPI dependencies.

        Resolved instances are cached on the container: subsequent calls
        reuse previously resolved dependencies without re-invoking them.

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
        instances: dict[Callable[..., Any], Any] = {}

        for dep in dependencies:
            resolved_callable = _resolve_callable(dep)
            instance = await self._resolve_single(resolved_callable)
            instances[resolved_callable] = instance

        return ResolvedDependencies(instances)

    async def aclose(self) -> None:
        """Close the container, running teardown for any ``yield`` dependencies."""
        await self._exit_stack.aclose()

    async def __aenter__(self) -> "FastAPIContainer":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _apply_overrides(self, call: Callable[..., Any]) -> Callable[..., Any]:
        """Apply dependency overrides, returning the substitute if one exists."""
        return self._dependency_overrides.get(call, call)

    async def _resolve_single(
        self,
        call: Callable[..., Any],
    ) -> Any:
        """Recursively resolve a single dependency and all its sub-dependencies."""
        if call in self._instance_cache:
            return self._instance_cache[call]

        # Apply overrides: if the original callable has an override, use it instead.
        effective_call = self._apply_overrides(call)

        dependant = _get_dependant(effective_call, self._dependant_cache)

        # Resolve sub-dependencies first.
        sub_values: dict[str, Any] = {}
        for sub_dep in dependant.dependencies:
            sub_call = cast("Callable[..., Any]", sub_dep.call)
            # Resolve the RegistrableDependency indirection that Depends may apply
            # at the Dependant level.
            sub_call = _resolve_callable(sub_call)
            sub_instance = await self._resolve_single(sub_call)
            if sub_dep.name is not None:
                sub_values[sub_dep.name] = sub_instance

        # Outside ASGI, request objects, headers, query/path params are not
        # available. Provide a stub Request and use declared defaults for
        # header/query/path params so the dependency chain works in standalone.
        sig = inspect.signature(effective_call)
        for param_name, param in sig.parameters.items():
            if param_name in sub_values:
                continue
            hint = param.annotation
            if (
                hint is not inspect.Parameter.empty
                and isinstance(hint, type)
                and issubclass(hint, HTTPConnection)
                and param.default is not None
            ):
                sub_values[param_name] = _STUB_REQUEST

        for param_field in (
            *dependant.header_params,
            *dependant.query_params,
            *dependant.path_params,
            *dependant.cookie_params,
        ):
            if param_field.name not in sub_values:
                sub_values[param_field.name] = param_field.field_info.default

        # Determine the callable's execution model from the call itself rather
        # than from ``Dependant`` attributes: the ``is_*_callable`` flags only
        # became attributes of ``Dependant`` in recent FastAPI, whereas these
        # module-level helpers have been stable across the supported range.
        async_gen = is_async_gen_callable(effective_call)
        sync_gen = is_gen_callable(effective_call)

        # Invoke the callable itself.
        if async_gen:
            cm = asynccontextmanager(effective_call)(**sub_values)
            instance = await self._exit_stack.enter_async_context(cm)
        elif sync_gen:
            cm = contextmanager_in_threadpool(
                contextmanager(effective_call)(**sub_values)
            )
            instance = await self._exit_stack.enter_async_context(cm)
        elif is_coroutine_callable(effective_call):
            instance = await effective_call(**sub_values)
        else:
            instance = await run_in_threadpool(effective_call, **sub_values)

        # Only cache non-generator dependencies at container level — generators
        # are tied to the exit_stack lifecycle of the container.
        if not (async_gen or sync_gen):
            self._instance_cache[call] = instance
        return instance


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
