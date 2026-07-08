"""Version-independent introspection of a callable's execution model.

FastAPI historically exposed ``is_async_gen_callable`` / ``is_gen_callable`` /
``is_coroutine_callable`` as functions in ``fastapi.dependencies.utils`` (older
releases) and later moved the information onto ``Dependant`` attributes,
removing the module-level helpers. To stay compatible across the whole
supported FastAPI range we reimplement the (small, stable) detection logic here
instead of importing it — it depends only on the standard library.
"""

import functools
import inspect
from collections.abc import Callable
from typing import Any

import fastapi.params

# ``scope`` was added to ``Depends.__init__`` only in recent FastAPI; detect it
# so callers can gate ``Depends(scope=...)`` usage on older releases.
DEPENDS_SUPPORTS_SCOPE = (
    "scope" in inspect.signature(fastapi.params.Depends.__init__).parameters
)


def _unwrap(call: Callable[..., Any]) -> Callable[..., Any]:
    while isinstance(call, functools.partial):
        call = call.func
    return call


def is_async_gen_callable(call: Callable[..., Any]) -> bool:
    """True if calling *call* returns an async generator."""
    call = _unwrap(call)
    if inspect.isasyncgenfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isasyncgenfunction(dunder_call)


def is_gen_callable(call: Callable[..., Any]) -> bool:
    """True if calling *call* returns a (sync) generator."""
    call = _unwrap(call)
    if inspect.isgeneratorfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isgeneratorfunction(dunder_call)


def is_coroutine_callable(call: Callable[..., Any]) -> bool:
    """True if *call* is a coroutine function (or a callable with one)."""
    call = _unwrap(call)
    if inspect.iscoroutinefunction(call):
        return True
    if inspect.isclass(call):
        return False
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.iscoroutinefunction(dunder_call)
