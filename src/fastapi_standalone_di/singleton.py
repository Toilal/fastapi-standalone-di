"""Application-lifetime singletons backed by :class:`AppState`.

:func:`singleton` turns a dependency factory into a singleton: its instance is
built once, cached in :class:`~fastapi_standalone_di.app_state.AppState`, and
reused thereafter. It behaves identically whether resolution is driven by
FastAPI (ASGI) or by :class:`~fastapi_standalone_di.resolve.FastAPIContainer`
(standalone), because the cache gate lives inside the callable both engines
invoke and the store is the shared ``AppState`` (``request.app.state`` in ASGI,
the module-level store standalone).

Two resolution modes are available:

* **eager** (default) — the wrapper keeps the factory's own ``Depends`` and adds
  an injected ``AppState``; the factory *body* runs at most once while its
  sub-dependency tree is re-resolved on every access (cheap sub-deps only).
  Generator factories are rejected: there is no application-lifetime owner for
  their teardown.
* **lazy** (``lazy=True``) — the wrapper delegates to ``container.get(factory)``,
  so the tree is resolved exactly once and the container owns any ``yield``
  teardown (run at ``aclose()`` == application shutdown). Requires a container
  registered in ``app_state`` (in ASGI, install one with
  :func:`~fastapi_standalone_di.resolve.container_lifespan`; standalone, the
  resolving container provides itself) — unless the value is preset under
  ``key``, which short-circuits construction before any container is needed.

Usage::

    @singleton(key="db")
    def get_db(settings: Settings = Depends(get_settings)) -> Database:
        return Database(settings.url)

    # ASGI:       Depends(get_db)          -> shared across requests via app.state
    # standalone: await container.get(get_db)  -> same AppState-backed store
"""

import asyncio
import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar, cast, get_type_hints, overload

from fastapi import Depends
from fastapi.concurrency import run_in_threadpool

from fastapi_standalone_di._compat import (
    is_async_gen_callable,
    is_coroutine_callable,
    is_gen_callable,
)
from fastapi_standalone_di.app_state import AppState, get_app_state

T = TypeVar("T")

# Distinguishes "not built yet" from a singleton whose value is legitimately
# ``None`` — the latter must be cached and reused, not rebuilt on every access.
_MISSING: Any = object()


def _default_key(factory: Callable[..., Any]) -> str:
    """A namespaced, collision-free ``AppState`` key derived from the factory."""
    module = getattr(factory, "__module__", "?")
    qualname = getattr(factory, "__qualname__", repr(factory))
    return f"__singleton__:{module}.{qualname}"


def _fresh_param_name(signature: inspect.Signature, base: str) -> str:
    """A parameter name of the form ``__fsd_<base>__`` not used by *signature*."""
    name = f"__fsd_{base}__"
    while name in signature.parameters:
        name += "_"
    return name


def _lock_from_state(app_state: AppState, key: str) -> asyncio.Lock:
    """Return (creating on first use) the per-key construction lock.

    The lock lives in the ``AppState`` so its lifetime tracks the store: it is
    shared by concurrent accesses under the same *key* and reset whenever the
    state is. Get-then-set is atomic here — no ``await`` between the miss and the
    store — so two coroutines never create two locks.
    """
    lock_key = f"__singleton_lock__:{key}"
    lock: asyncio.Lock | None = app_state.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        app_state.set(lock_key, lock)
    return lock


def _build_eager(factory: Callable[..., T], key: str) -> Callable[..., T]:
    """Build the eager-mode wrapper: gate on ``AppState`` inside the callable."""
    if is_gen_callable(factory) or is_async_gen_callable(factory):
        target = getattr(factory, "__qualname__", repr(factory))
        raise TypeError(
            f"singleton() cannot wrap generator dependency {target!r} in the "
            "default (eager) mode: there is no application-lifetime owner for its "
            "teardown. Use singleton(..., lazy=True) so the container owns it."
        )

    signature = inspect.signature(factory)
    for param in signature.parameters.values():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            target = getattr(factory, "__qualname__", repr(factory))
            raise TypeError(
                f"singleton() cannot wrap {target!r}: variadic (*args/**kwargs) "
                "factories are unsupported."
            )

    # Resolve annotations against the factory's own module so the merged
    # signature is correct even under ``from __future__ import annotations`` —
    # the wrapper's ``__globals__`` differ from the factory's.
    hints = get_type_hints(factory, include_extras=True)
    app_state_param = _fresh_param_name(signature, "app_state")
    factory_is_async = is_coroutine_callable(factory)

    async def wrapper(**kwargs: Any) -> Any:
        app_state: AppState = kwargs.pop(app_state_param)
        cached = app_state.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        async with _lock_from_state(app_state, key):
            cached = app_state.get(key, _MISSING)
            if cached is not _MISSING:
                return cached
            if factory_is_async:
                instance = await cast("Any", factory(**kwargs))
            else:
                instance = await run_in_threadpool(factory, **kwargs)
            app_state.set(key, instance)
            return instance

    functools.wraps(factory)(wrapper)
    params = [
        param.replace(annotation=hints.get(name, param.annotation))
        for name, param in signature.parameters.items()
    ]
    params.append(
        inspect.Parameter(
            app_state_param,
            inspect.Parameter.KEYWORD_ONLY,
            default=Depends(get_app_state),
            annotation=AppState,
        )
    )
    wrapper.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    return cast("Callable[..., T]", wrapper)


def _build_lazy(factory: Callable[..., T], key: str) -> Callable[..., T]:
    """Build the lazy-mode wrapper: delegate construction to the container."""
    from fastapi_standalone_di.resolve import (
        FastAPIContainer,
        _get_container_optional,
    )

    async def wrapper(
        app_state: AppState = Depends(get_app_state),
        container: FastAPIContainer | None = Depends(_get_container_optional),
    ) -> Any:
        cached = app_state.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        if container is None:
            target = getattr(factory, "__qualname__", repr(factory))
            raise RuntimeError(
                f"lazy singleton {target!r} needs a FastAPIContainer but none is "
                "registered in app_state. In ASGI, install one at startup — e.g. "
                "FastAPI(lifespan=container_lifespan) — or preset the value under "
                f"key {key!r} so construction is short-circuited."
            )
        instance = await container.get(factory)
        app_state.set(key, instance)
        return instance

    functools.wraps(factory)(wrapper)
    # Drop the ``__wrapped__`` link ``functools.wraps`` sets: FastAPI's
    # ``Dependant`` classifies a call by unwrapping it (``inspect.unwrap``), so a
    # link to a generator *factory* would make it treat this coroutine wrapper as
    # an (async) generator and try to iterate it. The explicit ``__signature__``
    # below likewise keeps FastAPI from introspecting the factory's parameters.
    del wrapper.__wrapped__  # type: ignore[attr-defined]
    # ``container`` depends on the non-raising ``_get_container_optional``: FastAPI
    # resolves it unconditionally (before the body), so a preset value can still
    # short-circuit — the wrapper raises only on a real cache miss with no
    # container. Under ``FastAPIContainer`` it is seeded to the resolving one.
    wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "app_state",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=Depends(get_app_state),
                annotation=AppState,
            ),
            inspect.Parameter(
                "container",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=Depends(_get_container_optional),
                annotation=FastAPIContainer | None,
            ),
        ]
    )
    return cast("Callable[..., T]", wrapper)


@overload
def singleton(
    factory: Callable[..., T],
    *,
    key: str | None = ...,
    lazy: bool = ...,
) -> Callable[..., T]: ...


@overload
def singleton(
    *,
    key: str | None = ...,
    lazy: bool = ...,
) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


def singleton(
    factory: Callable[..., T] | None = None,
    *,
    key: str | None = None,
    lazy: bool = False,
) -> Callable[..., T] | Callable[[Callable[..., T]], Callable[..., T]]:
    """Turn a dependency factory into an ``AppState``-backed singleton.

    Usable functionally (``get_db = singleton(build_db, key="db")``), as a bare
    decorator (``@singleton``), or a parametrised one (``@singleton(key="db")``).
    The returned callable is a drop-in dependency: use it with ``Depends(...)``
    in routes or ``container.get(...)`` standalone.

    Parameters
    ----------
    factory:
        The dependency callable whose result should be a singleton.
    key:
        ``AppState`` key under which the instance is cached. Defaults to a
        namespaced id derived from the factory. Share a key with
        :func:`~fastapi_standalone_di.app_state.set_app_state_value` to preset or
        override the instance (a preset short-circuits construction).
    lazy:
        When ``False`` (default) the factory body runs once but its
        sub-dependency tree is re-resolved on each access (eager mode); generator
        factories are rejected. When ``True`` construction is delegated to the
        container reachable via
        :func:`~fastapi_standalone_di.resolve.get_container`, resolving the tree
        exactly once and letting the container own ``yield`` teardown.
    """

    def decorate(fn: Callable[..., T]) -> Callable[..., T]:
        resolved_key = key if key is not None else _default_key(fn)
        if lazy:
            return _build_lazy(fn, resolved_key)
        return _build_eager(fn, resolved_key)

    if factory is None:
        return decorate
    return decorate(factory)
