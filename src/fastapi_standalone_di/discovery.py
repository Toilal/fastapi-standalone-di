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
from collections.abc import Callable, Iterator, Sequence
from types import ModuleType
from typing import Any, NamedTuple, cast

from fastapi_standalone_di.registration import RegistrableDependency
from fastapi_standalone_di.singleton import _SINGLETON_IMPL_ATTR

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
    matching implementation, and ambiguous interfaces (several candidates that no
    ``conflict_solver`` resolved). Nothing is registered when it is raised.
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


ConflictSolver = Callable[[type[RegistrableDependency], list[type]], type | None]


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

    :param packages: packages that may hold **both** interfaces and
        implementations, scanned once for both roles. Each is a dotted name or an
        imported module; a leading ``.`` is anchored to the caller's package,
        like :func:`register_bindings`.
    :param interfaces: extra packages scanned for interface classes only.
    :param implementations: extra packages scanned for implementation classes only.
    :param recursive: also descend into nested subpackages. Defaults to ``True``
        (unlike :func:`register_bindings`): implementations are typically spread
        across a subtree.
    :param conflict_solver: optional tie-breaker called once per interface that
        has two or more matching implementations, with the interface class and
        the ordered candidate classes. It returns the chosen candidate (must be
        one of them), or ``None`` to leave the ambiguity unresolved. Without it,
        an ambiguity is an error.
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

    An interface is bound to its single matching implementation. An interface
    that already carries its own implementation is left untouched and reported
    with ``already_bound=True``. Resolution and registration are two phases: if
    any interface has zero matches, or an unresolved ambiguity, nothing is
    registered and an :class:`AutoBindingError` aggregating every problem is
    raised.

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
    targets: dict[type, Callable[..., Any]] = {}
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
        impl = _impl_class(member)
        if impl is None or inspect.isabstract(impl):
            continue
        if _under_any(impl.__module__, implementation_names):
            candidates.append(impl)
            targets[impl] = member

    by_interface: dict[type, list[type]] = {interface: [] for interface in found}
    for impl in candidates:
        for base in impl.__bases__:
            if base in by_interface:
                by_interface[base].append(impl)

    planned: list[Binding] = []
    preexisting: list[Binding] = []
    unmatched: list[type] = []
    ambiguous: list[tuple[type, list[type]]] = []
    for interface in found:
        iface = cast("type[RegistrableDependency]", interface)
        own_impl = interface.__dict__.get("_impl")
        if own_impl is not None:
            preexisting.append(Binding(iface, own_impl, True))
            continue
        impls = sorted(by_interface[interface], key=_class_key)
        if len(impls) == 1:
            planned.append(Binding(iface, targets[impls[0]], False))
        elif not impls:
            unmatched.append(interface)
        elif conflict_solver is None:
            ambiguous.append((interface, impls))
        else:
            chosen = conflict_solver(iface, impls)
            if chosen is None:
                ambiguous.append((interface, impls))
            elif chosen not in impls:
                raise AutoBindingError(
                    f"conflict_solver returned {_qualname(chosen)}, which is not "
                    f"among the candidates for {_qualname(interface)}: "
                    f"{[_qualname(i) for i in impls]}"
                )
            else:
                planned.append(Binding(iface, targets[chosen], False))

    if unmatched or ambiguous:
        raise AutoBindingError(_format_problems(unmatched, ambiguous))

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
            if _impl_class(obj) is not None and obj.__module__ == module.__name__:
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
      one of those interfaces: only these can define a matching implementation.

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
    """

    defines_marker_subclass: bool
    base_names: frozenset[str]
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
    unresolved = False
    for node in ast.walk(tree):
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

    return _ModuleAnalysis(defines_marker, frozenset(base_names), unresolved)


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


def _class_key(cls: type) -> tuple[str, str]:
    return (cls.__module__, cls.__qualname__)


def _qualname(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _format_problems(
    unmatched: list[type], ambiguous: list[tuple[type, list[type]]]
) -> str:
    lines = ["auto_bindings could not wire every interface:"]
    for interface in sorted(unmatched, key=_class_key):
        lines.append(f"  - {_qualname(interface)}: no matching implementation")
    for interface, impls in sorted(ambiguous, key=lambda pair: _class_key(pair[0])):
        names = ", ".join(_qualname(impl) for impl in impls)
        lines.append(
            f"  - {_qualname(interface)}: several matching implementations ({names})"
        )
    return "\n".join(lines)
