"""Mark a factory *function* (or a class) as a ``RegistrableDependency`` implementation.

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
returned type itself when it is an interface. It may also be a generator's
element type — ``Iterator[X]`` / ``AsyncIterator[X]`` (or the ``Generator`` forms)
— for a factory with ``yield`` teardown: the yielded ``X`` is the interface it
provides. A return carrying no interface at all (``Any``/missing/unrelated) is a
reported ``AutoBindingError``, not silent.

``@provides`` only *tags* the callable; it does not change its behaviour. On a
factory function it is mandatory (a function has no bases to wire on); on a class
it is optional, since a class is wired by its hierarchy. But the tag also ranks
candidates when several match one interface: a ``@provides``-marked candidate — a
class or a factory — beats an unmarked implementation class, and
``@provides(primary=True)`` beats every other candidate. So a factory
automatically wins over a bare class, and ``primary=`` settles any remaining tie,
without a ``conflict_solver``. Combine it with
:func:`~fastapi_standalone_di.singleton.singleton` for an application-lifetime
implementation — the tag survives the wrapper, in either decorator order — or use
it alone for one rebuilt on every resolution.
"""

from collections.abc import Callable
from typing import NamedTuple, TypeVar, overload

T = TypeVar("T")

# Set on a callable (or class) by ``@provides`` to a ``_ProvidesConfig``. It marks
# an auto-bindable implementation and carries its options. ``auto_bindings`` reads
# it to treat a marked *function* as the implementation of the interface named by
# its return annotation, and to honour ``primary`` when breaking a tie.
# ``functools.wraps`` copies it onto a ``@singleton`` wrapper, so ``@singleton
# @provides`` composes in either order.
_PROVIDES_MARKER = "__fsd_provides__"


class _ProvidesConfig(NamedTuple):
    """Options a ``@provides`` marker carries.

    ``primary`` elects the candidate to bind when several match one interface,
    settling the ambiguity without a ``conflict_solver``.
    """

    primary: bool


@overload
def provides(fn: Callable[..., T]) -> Callable[..., T]: ...
@overload
def provides(
    *, primary: bool = ...
) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


def provides(
    fn: Callable[..., T] | None = None, *, primary: bool = False
) -> Callable[..., T] | Callable[[Callable[..., T]], Callable[..., T]]:
    """Mark *fn* as an implementation of the interface it provides.

    Usable bare (``@provides``) or parametrised (``@provides(primary=True)``). See
    the module docstring. On a factory *function*, ``auto_bindings`` binds the
    interface named by *fn*'s return annotation — the interface itself, one it
    implements, or a generator's element type — to *fn* (or, when *fn* is also
    ``@singleton``, to the singleton wrapper); a return annotation carrying no
    interface is reported as a misuse. Marking *fn* also ranks it: it beats an
    unmarked implementation class for a shared interface, and ``primary=True``
    beats every other candidate — on a function or a class alike.
    """

    def decorate(target: Callable[..., T]) -> Callable[..., T]:
        setattr(target, _PROVIDES_MARKER, _ProvidesConfig(primary=primary))
        return target

    if fn is None:
        return decorate
    return decorate(fn)
