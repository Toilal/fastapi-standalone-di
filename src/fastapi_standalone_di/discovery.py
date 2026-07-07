"""Discover per-package binding modules and run their ``register()``.

With :class:`~fastapi_standalone_di.registration.RegistrableDependency`, an
interface is bound to its implementation via ``Interface.register(Impl)``. The
natural home for those calls in a feature-oriented codebase is a per-feature
``di`` module::

    # myapp/features/orders/di.py
    def register() -> None:
        OrderService.register(DefaultOrderService)
        OrderRepository.register(SqlOrderRepository)

Because FastAPI resolves a route's full ``Depends(...)`` tree at decoration
time, every binding must be in place *before* the routers are mounted.
:func:`register_bindings` walks the subpackages of a package, imports each one's
binding module and calls its ``register()`` — the "wire everything up front"
step, so every ``Depends(Interface)`` resolves at route-decoration time.
"""

import importlib
import importlib.util
import logging
import pkgutil
import sys
from types import ModuleType

logger = logging.getLogger(__name__)


def register_bindings(
    *packages: str | ModuleType,
    module: str = "di",
    attr: str = "register",
    recursive: bool = False,
    warn_missing: bool = True,
) -> None:
    """Import each subpackage's binding module and call its registration callable.

    :param packages: the packages whose subpackages are scanned, each an
        imported module or a dotted name. Pass several to wire up more than one
        feature root in a single call. A name starting with ``.`` is relative to
        the calling module's package (``"."`` is that package itself,
        ``".features"`` a subpackage of it), like a ``from . import`` statement.
    :param module: the submodule to look for under each subpackage. May be a
        dotted path (e.g. ``"api.di"``) for projects that nest the bindings.
    :param attr: the callable to invoke on that module.
    :param recursive: also walk nested subpackages, not just the direct ones.
    :param warn_missing: emit a ``logging.warning`` when a matching module
        exists but exposes no callable ``attr`` (a malformed binding module),
        instead of failing silently at request time.

    Subpackages with no such module are skipped silently — a feature may
    legitimately declare no bindings. An import error raised *by* a binding
    module propagates: it is a real defect, surfaced here rather than at request
    time.

    :raises ValueError: if any of ``packages`` is not a package (has no
        ``__path__``).
    """
    anchor = (
        _caller_package()
        if any(isinstance(p, str) and p.startswith(".") for p in packages)
        else None
    )
    for package in packages:
        if isinstance(package, str):
            resolved = importlib.import_module(
                package, anchor if package.startswith(".") else None
            )
        else:
            resolved = package
        _walk(
            resolved,
            module=module,
            attr=attr,
            recursive=recursive,
            warn_missing=warn_missing,
        )


def _caller_package() -> str | None:
    """The package of the module that called :func:`register_bindings`.

    Used as the anchor for relative package names, mirroring what ``__package__``
    provides to a ``from . import`` statement in the caller's module.
    """
    globals_ = sys._getframe(2).f_globals
    package = globals_.get("__package__") or globals_.get("__name__")
    return str(package) if package else None


def _walk(
    package: ModuleType,
    *,
    module: str,
    attr: str,
    recursive: bool,
    warn_missing: bool,
) -> None:
    path = getattr(package, "__path__", None)
    if path is None:
        raise ValueError(
            f"{package.__name__!r} is not a package (no __path__ to iterate)"
        )
    for info in pkgutil.iter_modules(path, prefix=f"{package.__name__}."):
        if not info.ispkg:
            continue
        _register_from(info.name, module=module, attr=attr, warn_missing=warn_missing)
        if recursive:
            _walk(
                importlib.import_module(info.name),
                module=module,
                attr=attr,
                recursive=True,
                warn_missing=warn_missing,
            )


def _register_from(
    subpackage: str,
    *,
    module: str,
    attr: str,
    warn_missing: bool,
) -> None:
    name = f"{subpackage}.{module}"
    try:
        if importlib.util.find_spec(name) is None:
            return
    except ModuleNotFoundError:
        return
    imported = importlib.import_module(name)
    register = getattr(imported, attr, None)
    if not callable(register):
        if warn_missing:
            logger.warning("%s defines no callable %r", name, attr)
        return
    register()
