"""Mark a factory *function* as a ``RegistrableDependency`` implementation.

:func:`~fastapi_standalone_di.discovery.auto_bindings` wires implementation
*classes* to their interface by the class hierarchy — the interface is a direct
base. A factory *function* has no bases, so ``@provides`` marks it explicitly and
the interface it implements is read from its return annotation::

    @provides
    def build_config(store: ConfigStore = Depends(get_store)) -> ConfigState:
        return _FileConfigState(store)

    # auto_bindings: ConfigState -> build_config

The return annotation may be the interface itself or a concrete implementation of
it (a subclass); the function is then matched to the interface exactly as an
implementation class is — by the direct bases of the returned type, plus the
returned type itself when it is an interface. A return type carrying no interface
at all (``Any``, missing, or unrelated to ``RegistrableDependency``) is a misuse
``auto_bindings`` reports, since the marker promises an implementation.

``@provides`` only *tags* the callable; it does not change its behaviour. On a
class it is currently a redundant no-op — classes are already wired by their
hierarchy — but the tag is set all the same, so upcoming options (e.g.
``primary=True`` to settle an ambiguity) will apply to classes and functions
alike. Combine it with :func:`~fastapi_standalone_di.singleton.singleton` for an
application-lifetime implementation — the tag survives the wrapper, in either
decorator order — or use it alone for one rebuilt on every resolution.
"""

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

# Set on a callable by ``@provides`` to mark it an auto-bindable implementation.
# ``auto_bindings`` reads it to treat a marked *function* as the implementation of
# the interface named by its return annotation. ``functools.wraps`` copies it onto
# a ``@singleton`` wrapper, so ``@singleton @provides`` composes in either order.
_PROVIDES_MARKER = "__fsd_provides__"


def provides(fn: Callable[..., T]) -> Callable[..., T]:
    """Mark *fn* as the implementation of the interface it returns.

    See the module docstring. ``auto_bindings`` binds the interface named by
    *fn*'s return annotation — the interface itself, or one it implements — to
    *fn* (or, when *fn* is also ``@singleton``, to the singleton wrapper). A
    return annotation carrying no interface is reported as a misuse.
    """
    setattr(fn, _PROVIDES_MARKER, True)
    return fn
