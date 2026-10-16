"""Mark sync dependencies as safe to call directly on the event loop.

A dependency that is neither a coroutine nor a generator is called through the
threadpool, as FastAPI does: the library cannot tell whether its body blocks.
For a class whose ``__init__`` only stores its arguments, that hop costs far
more than the call itself. :func:`loop_safe` lets the author vouch for such a
callable so it is called inline instead.

The marker is the ``__fastapi_loop_safe__`` attribute, a contract shared with
``fastapi-cache-di``: an object marked by either library is recognised by both,
without one depending on the other. Its name must therefore never change.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi_standalone_di._compat import _impartial

LOOP_SAFE_ATTR = "__fastapi_loop_safe__"

T = TypeVar("T")


def loop_safe(obj: T) -> T:
    """Mark a sync function or a class as safe to call on the event loop.

    Returns *obj* itself, so ``Depends(obj)``, bindings and
    ``dependency_overrides[obj]`` keep working. A loop-safe callable must never
    block: no I/O, no lock, no heavy computation.
    """
    setattr(obj, LOOP_SAFE_ATTR, True)
    return obj


def is_loop_safe(call: Callable[..., Any]) -> bool:
    """True if *call* itself carries the loop-safe marker.

    The marker is read from the object's own namespace, never inherited: a
    subclass may redefine a blocking ``__init__`` and must be marked on its own.
    """
    namespace = getattr(_impartial(call), "__dict__", None)
    return namespace is not None and namespace.get(LOOP_SAFE_ATTR) is True
