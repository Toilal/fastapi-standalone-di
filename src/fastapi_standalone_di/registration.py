"""Registrable dependency interfaces for FastAPI.

``RegistrableDependency`` lets you declare an *interface* (an abstract base)
and depend on it via ``Depends(IMyService)`` while binding the concrete
implementation elsewhere with ``IMyService.register(MyServiceImpl)``. This
decouples the dependency declaration from its wiring, which is convenient both
inside FastAPI routes and when resolving dependencies standalone via
:class:`~fastapi_standalone_di.resolve.FastAPIContainer`.
"""

import inspect
from collections.abc import Callable
from inspect import isclass
from typing import Any, Literal

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

# ``scope`` was added to ``Depends.__init__`` only in recent FastAPI; detect it
# so ``_Depends`` stays constructible on older releases.
_DEPENDS_SUPPORTS_SCOPE = (
    "scope" in inspect.signature(FastAPIDepends.__init__).parameters
)


class _Depends(FastAPIDepends):
    """A ``Depends`` that dereferences a ``RegistrableDependency`` to its impl."""

    # The ``scope`` branch is resolved once, at class-definition time, rather
    # than on every instantiation.
    if _DEPENDS_SUPPORTS_SCOPE:

        def __init__(
            self,
            dependency: Callable[..., Any] | None = None,
            *,
            use_cache: bool = True,
            scope: Literal["function", "request"] | None = None,
        ):
            FastAPIDepends.__init__(self, dependency, use_cache=use_cache, scope=scope)
            self._dependency = dependency
    else:

        def __init__(
            self,
            dependency: Callable[..., Any] | None = None,
            *,
            use_cache: bool = True,
            scope: Literal["function", "request"] | None = None,
        ):
            FastAPIDepends.__init__(self, dependency, use_cache=use_cache)
            self._dependency = dependency

    @property
    def dependency(self) -> Callable[..., Any] | None:
        return (
            self._dependency.dependency()
            if isclass(self._dependency)
            and issubclass(self._dependency, RegistrableDependency)
            else self._dependency
        )

    @dependency.setter
    def dependency(self, value: Callable[..., Any] | None) -> None:
        self._dependency = value


def patch_for_registrable_dependency_support() -> bool:
    """Patch ``fastapi.params.Depends`` to resolve ``RegistrableDependency`` eagerly.

    Only needed when FastAPI itself must see the concrete implementation at
    introspection time (e.g. for OpenAPI). :class:`FastAPIContainer` resolves the
    indirection on its own and does not require this patch.

    Returns ``True`` if the patch was applied, ``False`` if already patched.
    """
    if FastAPIDepends == fastapi.params.Depends:
        fastapi.params.Depends = _Depends  # type: ignore[assignment,misc]
        return True
    return False
