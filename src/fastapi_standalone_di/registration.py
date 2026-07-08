"""Registrable dependency interfaces for FastAPI.

``RegistrableDependency`` lets you declare an *interface* (an abstract base)
and depend on it via ``Depends(IMyService)`` while binding the concrete
implementation elsewhere with ``IMyService.register(MyServiceImpl)``. This
decouples the dependency declaration from its wiring, which is convenient both
inside FastAPI routes and when resolving dependencies standalone via
:class:`~fastapi_standalone_di.resolve.FastAPIContainer`.
"""

from collections.abc import Callable
from inspect import isclass
from typing import Any

import fastapi.params


class classproperty(property):
    fget: Callable[[Any], Any]

    def __init__(self, fget: Callable[[Any], Any], *arg: Any, **kw: Any):
        super().__init__(fget, *arg, **kw)
        self.__doc__ = fget.__doc__

    def __get__(self, obj: Any, cls: type | None = None) -> Any:
        return self.fget(cls)


class RegistrableDependency:
    """Base class for a dependency interface with a swappable implementation."""

    _impl: Callable[..., Any] | None = None

    @classproperty
    def impl(cls) -> Callable[..., Any]:
        return cls.dependency()

    @classmethod
    def register(cls, impl: Callable[..., Any] | None) -> None:
        """Register (or clear, with ``None``) the implementation for this interface."""
        cls._impl = impl

    @classmethod
    def dependency(cls) -> Callable[..., Any]:
        """Entry point for ``fastapi.Depends``: return the registered implementation.

        Raises :class:`RuntimeError` when no implementation is registered — it
        never returns ``None``.
        """
        if cls._impl is None:
            raise RuntimeError(
                f"No implementation registered for {cls.__module__}.{cls.__name__}"
            )
        return cls._impl


FastAPIDepends = fastapi.params.Depends

# The ``dependency`` value FastAPI's ``Depends.__init__`` stores as a plain
# instance attribute; the patch rehomes it here so its own ``dependency``
# property can dereference it without recursing through itself.
_RAW_DEPENDENCY = "_fsd_dependency"

# Marks the class as already carrying the property, so the patch is idempotent.
_PATCHED_FLAG = "_fsd_registrable_patched"


def _resolve_registrable(raw: Callable[..., Any] | None) -> Callable[..., Any] | None:
    if isclass(raw) and issubclass(raw, RegistrableDependency):
        return raw.dependency()
    return raw


def _get_registrable_dependency(self: Any) -> Callable[..., Any] | None:
    state = self.__dict__
    if _RAW_DEPENDENCY in state:
        raw = state[_RAW_DEPENDENCY]
    else:
        raw = state.get("dependency")
    return _resolve_registrable(raw)


def _set_registrable_dependency(self: Any, value: Callable[..., Any] | None) -> None:
    self.__dict__[_RAW_DEPENDENCY] = value


# A data descriptor: it shadows the ``dependency`` entry that FastAPI's
# ``__init__`` writes into every instance's ``__dict__`` — including instances
# built *before* the patch — so the dereference applies to them too.
_registrable_dependency = property(
    _get_registrable_dependency, _set_registrable_dependency
)


def patch_for_registrable_dependency_support() -> bool:
    """Patch ``fastapi.params.Depends`` to resolve ``RegistrableDependency`` eagerly.

    Only needed when FastAPI itself must see the concrete implementation at
    introspection time (e.g. for OpenAPI). :class:`FastAPIContainer` resolves the
    indirection on its own and does not require this patch.

    The patch installs a ``dependency`` property **on the existing class in
    place** rather than swapping the class reference, so every ``Depends``
    instance is affected regardless of when it was built and every one keeps
    passing FastAPI's ``isinstance(_, fastapi.params.Depends)`` check. Order no
    longer matters: it works whether the ``Depends`` objects (a route's own, a
    :func:`~fastapi_standalone_di.singleton.singleton` wrapper's, the
    container's) were created before or after this call.

    Returns ``True`` if the patch was applied, ``False`` if already patched.
    """
    if getattr(FastAPIDepends, _PATCHED_FLAG, False):
        return False
    FastAPIDepends.dependency = _registrable_dependency  # type: ignore[assignment]
    setattr(FastAPIDepends, _PATCHED_FLAG, True)
    return True
