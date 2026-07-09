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
step, so every ``Depends(Interface)`` resolves at route-decoration time. Passing
a feature package directly also imports its *own* binding module, so an entry
point can wire an explicit subset of features rather than a whole subtree.
"""

import ast
import importlib
import importlib.util
import inspect
import logging
import os
import pkgutil
import sys
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Sequence,
)
from types import ModuleType
from typing import Any, NamedTuple, cast, get_args, get_origin, get_type_hints

from fastapi_standalone_di.provides import (
    _PROVIDES_MARKER,
    _ProvidesConfig,
    provides,
)
from fastapi_standalone_di.registration import RegistrableDependency
from fastapi_standalone_di.singleton import _SINGLETON_IMPL_ATTR

_PROVIDES_NAME = provides.__name__

# The generic origins a ``@provides`` generator factory annotates its yield with.
# ``get_origin`` normalises both the ``typing`` and ``collections.abc`` spellings
# to these, so ``Iterator[X]`` / ``AsyncIterator[X]`` (and the ``Generator`` forms)
# unwrap to their element type ``X`` — the interface the factory provides.
_GENERATOR_ORIGINS = frozenset(
    {
        Iterator,
        Iterable,
        Generator,
        AsyncIterator,
        AsyncIterable,
        AsyncGenerator,
    }
)

logger = logging.getLogger(__name__)


def register_bindings(
    *packages: str | ModuleType,
    module: str = "di",
    attr: str = "register",
    recursive: bool = False,
    warn_missing: bool = True,
) -> None:
    """Import each package's own and subpackages' binding modules and call their
    registration callable.

    :param packages: the packages whose own binding module and subpackages are
        wired, each an imported module or a dotted name. Each package's own
        ``<module>`` is imported (if any) *and* its subpackages are scanned, so
        passing feature roots wires whole subtrees while passing concrete
        feature packages wires exactly those. Pass several to wire up more than
        one target in a single call. A name starting with ``.`` is relative to
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
    seen: set[str] = set()
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
            seen=seen,
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
    seen: set[str],
    include_self: bool = True,
) -> None:
    path = getattr(package, "__path__", None)
    if path is None:
        raise ValueError(
            f"{package.__name__!r} is not a package (no __path__ to iterate)"
        )
    if include_self:
        _register_from(
            package.__name__,
            module=module,
            attr=attr,
            warn_missing=warn_missing,
            seen=seen,
        )
    for info in pkgutil.iter_modules(path, prefix=f"{package.__name__}."):
        if not info.ispkg:
            continue
        _register_from(
            info.name, module=module, attr=attr, warn_missing=warn_missing, seen=seen
        )
        if recursive:
            _walk(
                importlib.import_module(info.name),
                module=module,
                attr=attr,
                recursive=True,
                warn_missing=warn_missing,
                seen=seen,
                include_self=False,
            )


def _register_from(
    subpackage: str,
    *,
    module: str,
    attr: str,
    warn_missing: bool,
    seen: set[str],
) -> None:
    name = f"{subpackage}.{module}"
    if name in seen:
        return
    try:
        if importlib.util.find_spec(name) is None:
            return
    except ModuleNotFoundError:
        return
    seen.add(name)
    imported = importlib.import_module(name)
    register = getattr(imported, attr, None)
    if not callable(register):
        if warn_missing:
            logger.warning("%s defines no callable %r", name, attr)
        return
    register()


class AutoBindingError(ValueError):
    """Raised when :func:`auto_bindings` cannot wire every discovered interface.

    Aggregates all wiring gaps found in a single scan: interfaces with no
    matching implementation, ambiguous interfaces (several candidates that neither
    a ``primary`` marker nor a ``conflict_solver`` resolved), interfaces with two
    or more candidates marked ``@provides(primary=True)``, and ``@provides``
    functions carrying no interface return type. Nothing is registered when it is
    raised.
    """


class Binding(NamedTuple):
    """One resolved interface→implementation link reported by :func:`auto_bindings`.

    ``already_bound`` is ``True`` for an interface that carried an implementation
    before the call (left untouched, reported for completeness) and ``False`` for
    one bound by the call itself.
    """

    interface: type[RegistrableDependency]
    implementation: Callable[..., Any]
    already_bound: bool


ConflictSolver = Callable[
    [type[RegistrableDependency], list[Callable[..., Any]]],
    Callable[..., Any] | None,
]


def auto_bindings(
    *packages: str | ModuleType,
    interfaces: Sequence[str | ModuleType] = (),
    implementations: Sequence[str | ModuleType] = (),
    recursive: bool = True,
    conflict_solver: ConflictSolver | None = None,
    ast: bool = True,
) -> list[Binding]:
    """Wire ``RegistrableDependency`` interfaces to their implementations by convention.

    Scans the given packages for interface classes (those carrying
    ``RegistrableDependency`` as a **direct** base) and implementation classes,
    then binds each interface to the implementation that declares it as a
    **direct** base — the class-hierarchy equivalent of the hand-written
    ``register()`` calls that :func:`register_bindings` discovers.

    An implementation may also be a **factory function** marked with
    :func:`~fastapi_standalone_di.provides.provides`, matched by its return
    annotation (the interface, or a concrete implementation of it) since a
    function has no bases. Combined with
    :func:`~fastapi_standalone_di.singleton.singleton`, the singleton wrapper is
    what gets registered, so its gate survives. A ``@provides`` whose return type
    carries no interface at all (``Any``, missing, or unrelated to
    ``RegistrableDependency``) is reported as an error (see
    :class:`AutoBindingError`).

    :param packages: packages that may hold **both** interfaces and
        implementations, scanned once for both roles. Each is a dotted name or an
        imported module; a leading ``.`` is anchored to the caller's package,
        like :func:`register_bindings`.
    :param interfaces: extra packages scanned for interface classes only.
    :param implementations: extra packages scanned for implementation classes only.
    :param recursive: also descend into nested subpackages. Defaults to ``True``
        (unlike :func:`register_bindings`): implementations are typically spread
        across a subtree.
    :param conflict_solver: optional tie-breaker called once per interface still
        ambiguous after the ``primary`` and marked-over-unmarked rules (see below),
        with the interface class and the remaining contenders (each an
        implementation class or a ``@provides`` function). It returns the chosen
        candidate (must be one of them), or ``None`` to leave the ambiguity
        unresolved. Without it, such an ambiguity is an error.
    :param ast: pre-filter the scanned tree by static analysis so only modules
        that *can* hold an interface or an implementation are ever imported
        (default ``True``). Without it, every module in the scanned packages is
        imported to be inspected — running their import-time side effects (e.g.
        mounting routers). A module is kept only if its source declares a class
        based on ``RegistrableDependency`` (an interface) or on one of the
        discovered interfaces (an implementation); anything the analysis cannot
        resolve statically (unreadable source, a syntax error, a dynamically
        computed base class) is imported to stay safe. Interfaces or
        implementations synthesised at import time — via ``type(...)`` or a
        metaclass, with no ``class`` statement in the source — are invisible to
        this analysis and get skipped; set ``ast=False`` for such a codebase.

    An interface is bound to its single matching implementation. When several
    match, they are ranked before any ``conflict_solver`` is consulted:

    1. a candidate marked :func:`~fastapi_standalone_di.provides.provides` with
       ``primary=True`` (a class or a factory alike) wins outright; two or more
       primaries for one interface is an error;
    2. otherwise ``@provides``-marked candidates take priority over unmarked ones
       — a factory function is always marked, an implementation class only when
       decorated — so a lone marked candidate wins over any number of unmarked
       classes.

    Only candidates left tied at the winning rank reach the ``conflict_solver``
    (or, without one, an ambiguity error). An interface that already carries its
    own implementation is left untouched and reported with ``already_bound=True``.
    Resolution and registration are two phases: if any interface has zero matches,
    an unresolved ambiguity, several primaries, or a ``@provides`` carries no
    interface return type, nothing is registered and an :class:`AutoBindingError`
    aggregating every problem is raised.

    :returns: every discovered interface that ends up with an implementation —
        both freshly bound (``already_bound=False``) and pre-existing
        (``already_bound=True``) — ordered by the interface's module and
        qualified name.
    :raises AutoBindingError: if some interface cannot be wired, or the
        ``conflict_solver`` returns a value that is not one of the candidates.
    """
    specs = [*packages, *interfaces, *implementations]
    anchor = (
        _caller_package()
        if any(isinstance(s, str) and s.startswith(".") for s in specs)
        else None
    )
    interface_roots = _resolve_roots([*packages, *interfaces], anchor)
    implementation_roots = _resolve_roots([*packages, *implementations], anchor)
    interface_names = {module.__name__ for module in interface_roots}
    implementation_names = {module.__name__ for module in implementation_roots}

    found: list[type] = []
    candidates: list[type] = []
    provider_impls: list[tuple[Callable[..., Any], type]] = []
    provider_errors: list[tuple[Callable[..., Any], str]] = []
    targets: dict[Callable[..., Any], Callable[..., Any]] = {}
    members = _scan_members(
        interface_roots,
        implementation_roots,
        recursive=recursive,
        ast_prefilter=ast,
        interface_names=interface_names,
        implementation_names=implementation_names,
    )
    for member in members:
        if isinstance(member, type) and RegistrableDependency in member.__bases__:
            if _under_any(member.__module__, interface_names):
                found.append(member)
            continue
        role = _provides_role(member)
        if role is not None:
            if _under_any(member.__module__, implementation_names):
                kind, payload = role
                if kind == "impl":
                    provider_impls.append((member, payload))
                else:
                    provider_errors.append((member, payload))
            continue
        impl = _impl_class(member)
        if impl is None or inspect.isabstract(impl):
            continue
        if _under_any(impl.__module__, implementation_names):
            candidates.append(impl)
            targets[impl] = member

    by_interface: dict[type, list[Callable[..., Any]]] = {i: [] for i in found}
    for impl in candidates:
        for base in impl.__bases__:
            if base in by_interface:
                by_interface[base].append(impl)
    for provider, returned in provider_impls:
        matched_interfaces = _provides_interfaces(returned, by_interface)
        for interface in matched_interfaces:
            by_interface[interface].append(provider)
        if matched_interfaces:
            targets[provider] = provider

    planned: list[Binding] = []
    preexisting: list[Binding] = []
    unmatched: list[type] = []
    ambiguous: list[tuple[type, list[Callable[..., Any]]]] = []
    multiple_primary: list[tuple[type, list[Callable[..., Any]]]] = []
    for interface in found:
        iface = cast("type[RegistrableDependency]", interface)
        own_impl = interface.__dict__.get("_impl")
        if own_impl is not None:
            preexisting.append(Binding(iface, own_impl, True))
            continue
        impls = sorted(by_interface[interface], key=_class_key)
        if not impls:
            unmatched.append(interface)
            continue
        if len(impls) == 1:
            planned.append(Binding(iface, targets[impls[0]], False))
            continue
        primaries = [impl for impl in impls if _is_primary(impl)]
        if len(primaries) > 1:
            multiple_primary.append((interface, primaries))
            continue
        if len(primaries) == 1:
            planned.append(Binding(iface, targets[primaries[0]], False))
            continue
        marked = [impl for impl in impls if _is_marked(impl)]
        contenders = marked or impls
        if len(contenders) == 1:
            planned.append(Binding(iface, targets[contenders[0]], False))
            continue
        if conflict_solver is None:
            ambiguous.append((interface, contenders))
            continue
        chosen = conflict_solver(iface, contenders)
        if chosen is None:
            ambiguous.append((interface, contenders))
        elif chosen not in contenders:
            raise AutoBindingError(
                f"conflict_solver returned {_qualname(chosen)}, which is not "
                f"among the candidates for {_qualname(interface)}: "
                f"{[_qualname(i) for i in contenders]}"
            )
        else:
            planned.append(Binding(iface, targets[chosen], False))

    if unmatched or ambiguous or provider_errors or multiple_primary:
        raise AutoBindingError(
            _format_problems(unmatched, ambiguous, provider_errors, multiple_primary)
        )

    for binding in planned:
        binding.interface.register(binding.implementation)

    return sorted(planned + preexisting, key=lambda b: _class_key(b.interface))


def _resolve_roots(
    specs: Sequence[str | ModuleType], anchor: str | None
) -> list[ModuleType]:
    roots: list[ModuleType] = []
    seen: set[str] = set()
    for spec in specs:
        if isinstance(spec, str):
            module = importlib.import_module(
                spec, anchor if spec.startswith(".") else None
            )
        else:
            module = spec
        if module.__name__ not in seen:
            seen.add(module.__name__)
            roots.append(module)
    return roots


def _impl_class(member: object) -> type | None:
    """The implementation class *member* stands for, or ``None`` if it is neither.

    A plain class stands for itself. A ``@singleton``-decorated class is a callable
    that carries the wrapped class under ``_SINGLETON_IMPL_ATTR``; it stands for
    that class, so the interface is matched by the class's own bases while the
    wrapper is what gets registered.
    """
    if isinstance(member, type):
        return member
    wrapped = getattr(member, _SINGLETON_IMPL_ATTR, None)
    return wrapped if isinstance(wrapped, type) else None


def _return_class(factory: Callable[..., Any]) -> type | None:
    """The class a factory's return annotation resolves to, or ``None``.

    Resolves string annotations against the factory's own module (so it works
    under ``from __future__ import annotations``), and unwraps a generator's
    element type (``Iterator[X]`` / ``AsyncIterator[X]`` and the ``Generator``
    forms) for a ``yield`` factory. Anything that is not a plain class once
    resolved — a missing annotation, ``Any``, a union, an unresolvable forward
    reference — yields ``None``: the factory carries no class-shaped return type
    to match an interface on.
    """
    try:
        hints = get_type_hints(factory)
    except Exception:
        return None
    returned = _unwrap_generator(hints.get("return"))
    return returned if isinstance(returned, type) else None


def _unwrap_generator(annotation: Any) -> Any:
    """The yielded type of a generator/iterator annotation, else *annotation*.

    A ``@provides`` factory with ``yield`` teardown annotates its return as
    ``Iterator[X]`` / ``AsyncIterator[X]`` (or a ``Generator`` form), whose first
    type argument is the yielded ``X`` — the interface it provides. Any other
    annotation is returned unchanged.
    """
    if get_origin(annotation) in _GENERATOR_ORIGINS:
        args = get_args(annotation)
        if args:
            return args[0]
    return annotation


def _provides_role(member: object) -> tuple[str, Any] | None:
    """Classify a ``@provides``-marked function for auto-binding.

    A ``@provides`` function declares itself the implementation of the interface
    its return annotation names — the interface itself, or a concrete
    implementation of it — since a function has no bases to match on. When the
    function is also ``@singleton``, the wrapped factory sits under
    ``_SINGLETON_IMPL_ATTR`` and carries the real return annotation; the wrapper
    is what gets registered, so the singleton gate survives.

    - ``("impl", returned)`` — the return type is ``RegistrableDependency``-related
      (an interface or an implementation of one). The interfaces it actually
      binds are resolved from *returned* like an implementation class: its direct
      interface bases, plus *returned* itself when it is an interface.
    - ``("error", reason)`` — the marker promises an implementation, but the
      return type carries no interface at all (``Any``, missing, unresolvable, or
      unrelated to ``RegistrableDependency``); *reason* explains the misuse.
    - ``None`` — *member* is not a ``@provides`` function. A ``@provides`` *class*
      is one too — including a ``@singleton`` class, whose wrapper unwraps to a
      class: it is wired by its hierarchy like any class, so the marker is ignored
      here.
    """
    if isinstance(member, type) or not getattr(member, _PROVIDES_MARKER, False):
        return None
    underlying: Any = getattr(member, _SINGLETON_IMPL_ATTR, member)
    if isinstance(underlying, type):
        return None
    returned = _return_class(underlying)
    if returned is None:
        return (
            "error",
            "has no resolvable return type; annotate the interface it builds "
            "(or an implementation of that interface)",
        )
    if not issubclass(returned, RegistrableDependency):
        return (
            "error",
            f"returns {_qualname(returned)}, which is not a RegistrableDependency "
            "interface or an implementation of one",
        )
    return ("impl", returned)


def _provides_config(candidate: Callable[..., Any]) -> _ProvidesConfig | None:
    """The ``@provides`` marker carried by *candidate*, or ``None``.

    Read from a class's own ``__dict__`` — never an inherited marker, so a subclass
    does not inherit a parent's ``@provides`` — and directly from a function or
    ``@singleton`` wrapper (``functools.wraps`` copies the marker onto it).
    """
    if isinstance(candidate, type):
        marker = candidate.__dict__.get(_PROVIDES_MARKER)
    else:
        marker = getattr(candidate, _PROVIDES_MARKER, None)
    return cast("_ProvidesConfig | None", marker)


def _is_marked(candidate: Callable[..., Any]) -> bool:
    """Whether *candidate* carries a ``@provides`` marker at all.

    A factory function is always marked (that is how it becomes a candidate); an
    implementation class is marked only when explicitly decorated. Marked
    candidates take priority over unmarked ones when several match an interface.
    """
    return _provides_config(candidate) is not None


def _is_primary(candidate: Callable[..., Any]) -> bool:
    """Whether *candidate* is marked ``@provides(primary=True)``.

    Elects one implementation when several match an interface, ahead of the
    marked-over-unmarked priority (see :func:`_is_marked`).
    """
    config = _provides_config(candidate)
    return config is not None and config.primary


def _provides_interfaces(
    returned: type, by_interface: dict[type, list[Callable[..., Any]]]
) -> list[type]:
    """The discovered interfaces a ``@provides`` returning *returned* implements.

    Mirrors how an implementation class is matched — by *returned*'s direct
    interface bases — and additionally binds *returned* itself when it is one of
    the discovered interfaces (the function returning the interface directly).
    """
    interfaces = [returned] if returned in by_interface else []
    interfaces.extend(base for base in returned.__bases__ if base in by_interface)
    return interfaces


def _scan_members(
    interface_roots: list[ModuleType],
    implementation_roots: list[ModuleType],
    *,
    recursive: bool,
    ast_prefilter: bool,
    interface_names: set[str],
    implementation_names: set[str],
) -> list[Any]:
    modules = _select_modules(
        interface_roots,
        implementation_roots,
        recursive=recursive,
        ast_prefilter=ast_prefilter,
        interface_names=interface_names,
        implementation_names=implementation_names,
    )
    members: dict[str, Any] = {}
    for module in modules:
        for obj in vars(module).values():
            relevant = _impl_class(obj) is not None or _provides_role(obj) is not None
            if relevant and obj.__module__ == module.__name__:
                members[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(members.values())


def _select_modules(
    interface_roots: list[ModuleType],
    implementation_roots: list[ModuleType],
    *,
    recursive: bool,
    ast_prefilter: bool,
    interface_names: set[str],
    implementation_names: set[str],
) -> list[ModuleType]:
    """The modules to inspect for members, importing each exactly once.

    Without ``ast_prefilter`` every module in the scanned tree is imported (the
    exhaustive walk). With it, only modules a static analysis cannot rule out as
    holding an interface or an implementation are imported (see
    :func:`_select_modules_ast`).
    """
    roots: dict[str, ModuleType] = {}
    for root in (*interface_roots, *implementation_roots):
        roots.setdefault(root.__name__, root)

    if not ast_prefilter:
        visited: set[str] = set()
        modules: list[ModuleType] = []
        for root in roots.values():
            modules.extend(_walk_modules(root, recursive=recursive, visited=visited))
        return modules

    return _select_modules_ast(
        list(roots.values()),
        recursive=recursive,
        interface_names=interface_names,
        implementation_names=implementation_names,
    )


def _select_modules_ast(
    roots: list[ModuleType],
    *,
    recursive: bool,
    interface_names: set[str],
    implementation_names: set[str],
) -> list[ModuleType]:
    """Import only the descendant modules that may declare an interface or impl.

    The scanned roots are always imported and inspected (they were passed
    explicitly). Their descendants are enumerated straight from the filesystem —
    never imported to enumerate them — and each is parsed:

    - Pass 1 keeps every module whose source declares a class based on
      ``RegistrableDependency``: only these can define an interface. Importing
      them yields the concrete interface classes.
    - Pass 2 keeps every remaining module whose source declares a class based on
      one of those interfaces (a concrete implementation) or decorates a function
      with ``@provides`` (a factory implementation, whatever it returns — so a
      mis-annotated one is imported and reported, not skipped silently).

    A module whose source cannot be read or parsed, or that carries a base class
    the analysis cannot resolve statically, is imported unconditionally — the
    filter only ever skips a module it can prove holds nothing relevant.
    """
    inventory: dict[str, bytes | None] = {}
    for root in roots:
        path = getattr(root, "__path__", None)
        if path is None:
            continue
        for name, source in _iter_descendant_sources(
            list(path), f"{root.__name__}.", recursive
        ):
            inventory.setdefault(name, source)

    analyses = {name: _analyze_module(source) for name, source in inventory.items()}
    imported: dict[str, ModuleType] = {}

    for name in inventory:
        if not _under_any(name, interface_names):
            continue
        analysis = analyses[name]
        if (
            analysis is None
            or analysis.defines_marker_subclass
            or analysis.has_unresolved_base
        ):
            imported.setdefault(name, importlib.import_module(name))

    interface_class_names: set[str] = set()
    for module in (*roots, *imported.values()):
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and RegistrableDependency in obj.__bases__
                and obj.__module__ == module.__name__
            ):
                interface_class_names.add(obj.__name__)

    for name in inventory:
        if name in imported or not _under_any(name, implementation_names):
            continue
        analysis = analyses[name]
        if (
            analysis is None
            or analysis.has_unresolved_base
            or analysis.has_provider
            or analysis.base_names & interface_class_names
        ):
            imported.setdefault(name, importlib.import_module(name))

    return [*roots, *imported.values()]


def _iter_descendant_sources(
    search_paths: list[str], prefix: str, recursive: bool
) -> Iterator[tuple[str, bytes | None]]:
    """Yield ``(module_name, source_bytes | None)`` for every descendant module.

    Walks the package tree from the filesystem alone, importing nothing. Source
    is ``None`` when it cannot be located as a plain ``.py`` file (a namespace
    package's missing ``__init__``, a compiled-only or zipped module, an exotic
    finder) — the caller then imports that module rather than risk skipping it.
    """
    for info in pkgutil.iter_modules(search_paths, prefix=prefix):
        leaf = info.name.rpartition(".")[2]
        base = getattr(info.module_finder, "path", None)
        if info.ispkg:
            package_dir = os.path.join(base, leaf) if base is not None else None
            init = (
                os.path.join(package_dir, "__init__.py")
                if package_dir is not None
                else None
            )
            yield info.name, _read_source(init)
            if recursive and package_dir is not None:
                yield from _iter_descendant_sources(
                    [package_dir], f"{info.name}.", recursive
                )
        else:
            source_file = os.path.join(base, f"{leaf}.py") if base is not None else None
            yield info.name, _read_source(source_file)


def _read_source(path: str | None) -> bytes | None:
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


class _ModuleAnalysis(NamedTuple):
    """What a module's source statically reveals about the classes it declares.

    ``base_names`` holds, per class base, the *original* imported simple name
    (an ``as`` alias is resolved back), so it can be matched against the
    discovered interface class names regardless of how the module aliased them.
    ``has_provider`` is ``True`` when the source decorates a function with
    ``@provides`` (the alias resolved back), so a factory implementation is found
    whatever its return annotation.
    """

    defines_marker_subclass: bool
    base_names: frozenset[str]
    has_provider: bool
    has_unresolved_base: bool


def _analyze_module(source: bytes | None) -> _ModuleAnalysis | None:
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    aliases: dict[str, str] = {}
    star_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star_import = True
                else:
                    aliases[alias.asname or alias.name] = alias.name

    base_names: set[str] = set()
    defines_marker = False
    has_provider = False
    unresolved = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                simple = _base_simple_name(target)
                if simple is not None and aliases.get(simple, simple) == _PROVIDES_NAME:
                    has_provider = True
            continue
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            simple = _base_simple_name(base)
            if simple is None:
                unresolved = True
                continue
            original = aliases.get(simple, simple)
            base_names.add(original)
            if original == RegistrableDependency.__name__:
                defines_marker = True
            elif star_import and simple not in aliases:
                unresolved = True

    return _ModuleAnalysis(
        defines_marker, frozenset(base_names), has_provider, unresolved
    )


def _base_simple_name(node: ast.expr) -> str | None:
    """The bare class name a base-class expression ultimately references.

    Resolves ``Name`` and dotted ``Attribute`` accesses, and unwraps a
    subscripted generic (``Interface[T]`` → ``Interface``). Anything else — a
    call, an unpack, a computed expression — returns ``None``: the base cannot
    be matched statically, so the module is imported to be safe.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _base_simple_name(node.value)
    return None


def _walk_modules(
    package: ModuleType, *, recursive: bool, visited: set[str]
) -> Iterator[ModuleType]:
    if package.__name__ in visited:
        return
    visited.add(package.__name__)
    yield package
    path = getattr(package, "__path__", None)
    if path is None:
        return
    for info in pkgutil.iter_modules(path, prefix=f"{package.__name__}."):
        if info.name in visited:
            continue
        submodule = importlib.import_module(info.name)
        if info.ispkg and recursive:
            yield from _walk_modules(submodule, recursive=recursive, visited=visited)
        else:
            visited.add(info.name)
            yield submodule


def _under_any(module_name: str, roots: set[str]) -> bool:
    return any(
        module_name == root or module_name.startswith(f"{root}.") for root in roots
    )


def _class_key(obj: Any) -> tuple[str, str]:
    return (obj.__module__, obj.__qualname__)


def _qualname(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def _format_problems(
    unmatched: list[type],
    ambiguous: list[tuple[type, list[Callable[..., Any]]]],
    provider_errors: list[tuple[Callable[..., Any], str]],
    multiple_primary: list[tuple[type, list[Callable[..., Any]]]],
) -> str:
    lines = ["auto_bindings could not wire every interface:"]
    for interface in sorted(unmatched, key=_class_key):
        lines.append(f"  - {_qualname(interface)}: no matching implementation")
    for interface, impls in sorted(ambiguous, key=lambda pair: _class_key(pair[0])):
        names = ", ".join(_qualname(impl) for impl in impls)
        lines.append(
            f"  - {_qualname(interface)}: several matching implementations ({names})"
        )
    for interface, impls in sorted(
        multiple_primary, key=lambda pair: _class_key(pair[0])
    ):
        names = ", ".join(_qualname(impl) for impl in impls)
        lines.append(
            f"  - {_qualname(interface)}: several implementations marked "
            f"@provides(primary=True) ({names})"
        )
    for provider, reason in sorted(
        provider_errors, key=lambda entry: _class_key(entry[0])
    ):
        lines.append(f"  - {_qualname(provider)}: @provides {reason}")
    return "\n".join(lines)
