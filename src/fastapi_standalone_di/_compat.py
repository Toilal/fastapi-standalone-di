"""Version-independent introspection of a callable's execution model.

FastAPI's own helpers moved from ``fastapi.dependencies.utils`` to private
functions of ``fastapi.dependencies.models``, and their logic changed along the
way: ``functools.partial`` and ``functools.wraps`` wrappers are only unwrapped
since FastAPI 0.123.5. This module reimplements the detection of FastAPI
0.143.1 so the container classifies a dependency the way a current FastAPI does
in a request, whatever FastAPI version is installed.
"""

import functools
import inspect
import sys
from collections.abc import Callable
from typing import Any

import fastapi.params

if sys.version_info >= (3, 13):
    from inspect import iscoroutinefunction
else:
    from asyncio import iscoroutinefunction

# ``scope`` was added to ``Depends.__init__`` only in recent FastAPI; detect it
# so callers can gate ``Depends(scope=...)`` usage on older releases.
DEPENDS_SUPPORTS_SCOPE = (
    "scope" in inspect.signature(fastapi.params.Depends.__init__).parameters
)


def _impartial(call: Callable[..., Any]) -> Callable[..., Any]:
    while isinstance(call, functools.partial):
        call = call.func
    return call


def _unwrapped_call(call: Callable[..., Any]) -> Any:
    return inspect.unwrap(_impartial(call))


def _is_routine_coroutine(call: Any) -> bool:
    return inspect.isroutine(call) and iscoroutinefunction(call)


def _classify(
    call: Callable[..., Any],
    direct: Callable[[Any], bool],
    dunder: Callable[[Any], bool],
) -> bool:
    """Apply FastAPI's lookup order: the call itself, then its ``__call__``.

    Each object is tested both with ``functools.partial`` unwrapped and with
    ``functools.wraps`` wrappers unwrapped too, and a class is never classified
    through its ``__call__``.
    """
    impartial = _impartial(call)
    unwrapped = _unwrapped_call(call)
    if direct(impartial) or direct(unwrapped):
        return True
    if inspect.isclass(unwrapped):
        return False
    for target in (impartial, unwrapped):
        dunder_call = getattr(target, "__call__", None)  # noqa: B004
        if dunder_call is None:
            return False
        if dunder(_impartial(dunder_call)) or dunder(_unwrapped_call(dunder_call)):
            return True
    return False


def is_async_gen_callable(call: Callable[..., Any]) -> bool:
    """True if calling *call* returns an async generator."""
    return _classify(call, inspect.isasyncgenfunction, inspect.isasyncgenfunction)


def is_gen_callable(call: Callable[..., Any]) -> bool:
    """True if calling *call* returns a (sync) generator."""
    return _classify(call, inspect.isgeneratorfunction, inspect.isgeneratorfunction)


def is_coroutine_callable(call: Callable[..., Any]) -> bool:
    """True if calling *call* returns an awaitable FastAPI awaits."""
    return _classify(call, _is_routine_coroutine, iscoroutinefunction)
