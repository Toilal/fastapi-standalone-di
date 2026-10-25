"""Tests for fastapi_standalone_di.discovery.auto_bindings."""

import ast
import importlib
import json
import logging
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import fastapi.params
import pytest

from fastapi_standalone_di import (
    AppState,
    AutoBindingError,
    Binding,
    FastAPIContainer,
    RegistrableDependency,
    __version__,
    auto_bindings,
    discovery,
    patch_for_registrable_dependency_support,
)
from fastapi_standalone_di.registration import _PATCHED_FLAG

_ROOT = "_ab_pkg"


@pytest.fixture
def _unpatch_depends() -> Iterator[None]:
    """Undo the in-place ``Depends`` patch so the sweep starts from a clean class."""
    depends = fastapi.params.Depends
    had_property = "dependency" in depends.__dict__
    try:
        yield
    finally:
        if not had_property and "dependency" in depends.__dict__:
            del depends.dependency
        if hasattr(depends, _PATCHED_FLAG):
            delattr(depends, _PATCHED_FLAG)


@pytest.fixture
def make_package(tmp_path: Path) -> Iterator[Callable[[dict[str, str]], str]]:
    """Materialise a package tree from ``{relative/path.py: source}`` under a
    unique root package, put it on ``sys.path``, and clean everything up."""

    def build(tree: dict[str, str]) -> str:
        root_dir = tmp_path / _ROOT
        root_dir.mkdir(exist_ok=True)
        (root_dir / "__init__.py").write_text("")
        for rel, source in tree.items():
            target = root_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            for parent in target.parents:
                if parent == tmp_path:
                    break
                init = parent / "__init__.py"
                if not init.exists():
                    init.write_text("")
            target.write_text(source)
        sys.path.insert(0, str(tmp_path))
        return _ROOT

    yield build

    sys.path[:] = [p for p in sys.path if p != str(tmp_path)]
    for name in list(sys.modules):
        if name == _ROOT or name.startswith(f"{_ROOT}."):
            del sys.modules[name]


def _iface(*names: str) -> str:
    body = "\n".join(f"class {name}(RegistrableDependency): ..." for name in names)
    return f"from fastapi_standalone_di import RegistrableDependency\n\n\n{body}\n"


def _impl(name: str, base: str, base_module: str) -> str:
    return f"from {_ROOT}.{base_module} import {base}\n\n\nclass {name}({base}): ...\n"


def _singleton_impl(
    name: str, base: str, base_module: str, *, lazy: bool = False
) -> str:
    decorator = "@singleton(lazy=True)" if lazy else "@singleton"
    return (
        "from fastapi_standalone_di import singleton\n"
        f"from {_ROOT}.{base_module} import {base}\n\n\n"
        f"{decorator}\n"
        f"class {name}({base}):\n"
        "    def __init__(self) -> None: ...\n"
    )


def _provides_factory(
    name: str,
    returns: str,
    returns_module: str,
    *,
    singleton: bool = False,
    lazy: bool = False,
    primary: bool = False,
    gen: str | None = None,
) -> str:
    """A ``@provides`` factory *function* returning ``returns`` — an interface or a
    concrete implementation of one — optionally also a ``@singleton``.

    ``primary`` marks it ``@provides(primary=True)``. ``gen`` makes it a generator
    with ``yield`` teardown whose element type is ``returns``: ``"sync"`` annotates
    ``Iterator[...]``, ``"async"`` ``AsyncIterator[...]``."""
    imports = ["from fastapi_standalone_di import provides"]
    decorators = ["@provides(primary=True)" if primary else "@provides"]
    if singleton:
        imports.append("from fastapi_standalone_di import singleton")
        decorators.insert(0, "@singleton(lazy=True)" if lazy else "@singleton")
    imports.append(f"from {_ROOT}.{returns_module} import {returns}")

    if gen is None:
        annotation, statement, prefix = returns, f"return {returns}()", "def"
    else:
        wrapper = "AsyncIterator" if gen == "async" else "Iterator"
        imports.insert(0, f"from collections.abc import {wrapper}")
        annotation = f"{wrapper}[{returns}]"
        statement = f"yield {returns}()"
        prefix = "async def" if gen == "async" else "def"

    head = "\n".join(imports) + "\n\n\n" + "\n".join(decorators) + "\n"
    return f"{head}{prefix} {name}() -> {annotation}:\n    {statement}\n"


def _provides_class(
    name: str, base: str, base_module: str, *, primary: bool = False
) -> str:
    """An implementation *class* decorated with ``@provides`` (optionally primary)."""
    decorator = "@provides(primary=True)" if primary else "@provides"
    return (
        "from fastapi_standalone_di import provides\n"
        f"from {_ROOT}.{base_module} import {base}\n\n\n"
        f"{decorator}\n"
        f"class {name}({base}): ...\n"
    )


def _trap(message: str = "module must not be imported") -> str:
    """A module that raises on import — proof it was (not) imported."""
    return f"raise RuntimeError({message!r})\n"


def _cls(root: str, module: str, name: str) -> type:
    return getattr(importlib.import_module(f"{root}.{module}"), name)


class TestAutoBindings:
    def test_binds_single_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(
            interfaces=[f"{root}.contracts"], implementations=[f"{root}.infra"]
        )
        icache = _cls(root, "contracts.cache", "ICache")
        redis = _cls(root, "infra.redis", "RedisCache")
        assert result == [Binding(icache, redis, False)]
        assert icache.impl is redis

    def test_shared_positional_package(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(root)
        assert [b.interface.__name__ for b in result] == ["ICache"]
        assert result[0].implementation.__name__ == "RedisCache"

    def test_module_object_argument(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(importlib.import_module(root))
        assert result[0].interface.__name__ == "ICache"

    def test_relative_package_is_anchored_to_caller(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "assemble.py": (
                    "from fastapi_standalone_di import auto_bindings\n\n\n"
                    "def run():\n"
                    "    return auto_bindings('.')\n"
                ),
            }
        )
        result = importlib.import_module(f"{root}.assemble").run()
        assert result[0].implementation.__name__ == "RedisCache"

    def test_zero_implementation_raises(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package({"contracts/cache.py": _iface("ICache")})
        with pytest.raises(AutoBindingError, match="no matching implementation"):
            auto_bindings(root)

    def test_ambiguous_raises_naming_competitors(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/a.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/b.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )
        with pytest.raises(AutoBindingError, match="several matching") as excinfo:
            auto_bindings(root)
        assert "RedisCache" in str(excinfo.value)
        assert "MemCache" in str(excinfo.value)

    def test_conflict_solver_selects_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/a.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/b.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )

        def solver(
            interface: type[RegistrableDependency], impls: list[type]
        ) -> type | None:
            return next(i for i in impls if i.__name__ == "RedisCache")

        result = auto_bindings(root, conflict_solver=solver)
        icache = _cls(root, "contracts.cache", "ICache")
        assert result == [Binding(icache, icache.impl, False)]
        assert icache.impl.__name__ == "RedisCache"

    def test_conflict_solver_returning_none_keeps_error(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/a.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/b.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )

        def solver(
            interface: type[RegistrableDependency], impls: list[type]
        ) -> type | None:
            return None

        with pytest.raises(AutoBindingError, match="several matching"):
            auto_bindings(root, conflict_solver=solver)

    def test_conflict_solver_foreign_return_raises(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/a.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/b.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )

        def solver(
            interface: type[RegistrableDependency], impls: list[type]
        ) -> type | None:
            return int

        with pytest.raises(AutoBindingError, match="not among the candidates"):
            auto_bindings(root, conflict_solver=solver)

    def test_conflict_solver_exception_propagates(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/a.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/b.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )

        def solver(
            interface: type[RegistrableDependency], impls: list[type]
        ) -> type | None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            auto_bindings(root, conflict_solver=solver)

    def test_already_bound_reported_not_rebound(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        icache = _cls(root, "contracts.cache", "ICache")

        def manual() -> None: ...

        icache.register(manual)
        result = auto_bindings(root)
        assert result == [Binding(icache, manual, True)]
        assert icache.impl is manual

    def test_already_bound_skips_ambiguity(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/a.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/b.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )
        icache = _cls(root, "contracts.cache", "ICache")

        def manual() -> None: ...

        icache.register(manual)
        result = auto_bindings(root)
        assert result == [Binding(icache, manual, True)]

    def test_transitive_subclass_is_not_matched(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/tiered.py": _impl("TieredCache", "RedisCache", "infra.redis"),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        redis = _cls(root, "infra.redis", "RedisCache")
        assert result == [Binding(icache, redis, False)]

    def test_intermediate_without_marker_is_not_an_interface(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # An abstract intermediate base carries no direct marker: it is neither an
        # interface to bind (rule 1) nor an implementation candidate (abstract,
        # rule 5). Only IStore is wired.
        root = make_package(
            {
                "contracts/cache.py": (
                    "import abc\n"
                    "from fastapi_standalone_di import RegistrableDependency\n\n\n"
                    "class IStore(RegistrableDependency): ...\n\n\n"
                    "class ICache(IStore, abc.ABC):\n"
                    "    @abc.abstractmethod\n"
                    "    def get(self) -> object: ...\n"
                ),
                "infra/store.py": _impl("SqlStore", "IStore", "contracts.cache"),
            }
        )
        result = auto_bindings(root)
        istore = _cls(root, "contracts.cache", "IStore")
        sql = _cls(root, "infra.store", "SqlStore")
        assert result == [Binding(istore, sql, False)]
        assert all(b.interface.__name__ != "ICache" for b in result)

    def test_redeclared_marker_opts_interface_back_in(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": (
                    "from fastapi_standalone_di import RegistrableDependency\n\n\n"
                    "class IStore(RegistrableDependency): ...\n"
                    "class ICache(IStore, RegistrableDependency): ...\n"
                ),
                "infra/store.py": _impl("SqlStore", "IStore", "contracts.cache"),
                "infra/cache.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(root)
        pairs = sorted(
            (b.interface.__name__, b.implementation.__name__) for b in result
        )
        assert pairs == [("ICache", "RedisCache"), ("IStore", "SqlStore")]

    def test_implementation_with_several_interfaces_binds_all(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/svc.py": _iface("IA", "IB"),
                "infra/impl.py": (
                    f"from {_ROOT}.contracts.svc import IA, IB\n\n\n"
                    "class Thing(IA, IB): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        assert sorted(b.interface.__name__ for b in result) == ["IA", "IB"]
        assert all(b.implementation.__name__ == "Thing" for b in result)

    def test_is_atomic_on_error(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/svc.py": _iface("IA", "IB"),
                "infra/impl.py": _impl("AImpl", "IA", "contracts.svc"),
            }
        )
        ia = _cls(root, "contracts.svc", "IA")
        with pytest.raises(AutoBindingError):
            auto_bindings(root)
        assert ia._impl is None

    def test_recursive_default_finds_nested_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/__init__.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts"),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "RedisCache"

    def test_non_recursive_misses_nested_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/__init__.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts"),
            }
        )
        with pytest.raises(AutoBindingError, match="no matching"):
            auto_bindings(root, recursive=False)

    def test_overlapping_sources_bind_interface_once(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(root, interfaces=[f"{root}.contracts"])
        assert len(result) == 1
        assert result[0].interface.__name__ == "ICache"

    async def test_binds_singleton_decorated_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A ``@singleton``-decorated implementation is a callable, not a class, yet
        # it is discovered by the class it wraps and the *wrapper* is registered —
        # so the singleton gate survives instead of being bound away.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _singleton_impl(
                    "RedisCache", "ICache", "contracts.cache"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        wrapper = icache.impl
        assert result == [Binding(icache, wrapper, False)]
        assert not isinstance(wrapper, type)
        assert wrapper.__name__ == "RedisCache"

        AppState.reset_standalone()
        try:
            async with FastAPIContainer() as container:
                first = await container.get(icache.dependency())
                second = await container.get(icache.dependency())
        finally:
            AppState.reset_standalone()
        assert first is second
        assert type(first).__name__ == "RedisCache"

    async def test_binds_lazy_singleton_decorated_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A lazy @singleton implementation resolves via the container, which owns
        # its teardown. auto_bindings wires it like any other: at resolution the
        # interface dereferences to the lazy wrapper, and the wrapper builds the
        # concrete class directly (no re-entry through the interface).
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _singleton_impl(
                    "RedisCache", "ICache", "contracts.cache", lazy=True
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        assert result == [Binding(icache, icache.impl, False)]
        assert not isinstance(icache.impl, type)
        assert icache.impl.__name__ == "RedisCache"

        AppState.reset_standalone()
        try:
            async with FastAPIContainer() as container:
                first = await container.get(icache)
                second = await container.get(icache)
        finally:
            AppState.reset_standalone()
        assert first is second
        assert type(first).__name__ == "RedisCache"

    def test_unmatched_lazy_singleton_is_ignored(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A lazy singleton that is not the implementation of any wired interface
        # is ignored like any other unmatched candidate.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "infra/lazy.py": (
                    "from fastapi_standalone_di import singleton\n\n\n"
                    "class Base:\n"
                    "    def __init__(self) -> None: ...\n\n\n"
                    "@singleton(lazy=True)\n"
                    "class Standalone(Base): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        assert result == [Binding(icache, icache.impl, False)]
        assert icache.impl.__name__ == "RedisCache"

    def test_conflict_solver_sees_singleton_underlying_class(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # The solver receives the wrapped classes (real types it can inspect by
        # base/name), while the wrapper is what ends up registered for its pick.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _singleton_impl(
                    "RedisCache", "ICache", "contracts.cache"
                ),
                "infra/mem.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )
        seen: list[type] = []

        def solver(
            interface: type[RegistrableDependency], impls: list[type]
        ) -> type | None:
            seen.extend(impls)
            return next(i for i in impls if i.__name__ == "RedisCache")

        result = auto_bindings(root, conflict_solver=solver)
        icache = _cls(root, "contracts.cache", "ICache")
        assert all(isinstance(i, type) for i in seen)
        assert {i.__name__ for i in seen} == {"RedisCache", "MemCache"}
        assert result == [Binding(icache, icache.impl, False)]
        assert not isinstance(icache.impl, type)

    def test_binds_provides_factory_returning_interface(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A ``@provides`` factory function has no bases, so it is matched by its
        # return annotation being the interface. Used alone (no @singleton), the
        # function itself is registered and rebuilt on every resolution.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        build_cache = _cls(root, "infra.factory", "build_cache")
        assert result == [Binding(icache, build_cache, False)]
        assert icache.impl is build_cache

    async def test_binds_singleton_provides_factory(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # ``@singleton @provides`` composes: @provides marks it an implementation,
        # @singleton wraps it, and the wrapper (carrying the propagated mark) is
        # what gets registered — so the singleton gate survives.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache", singleton=True
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        wrapper = icache.impl
        assert result == [Binding(icache, wrapper, False)]
        assert not isinstance(wrapper, type)
        assert wrapper.__name__ == "build_cache"

        AppState.reset_standalone()
        try:
            async with FastAPIContainer() as container:
                first = await container.get(icache.dependency())
                second = await container.get(icache.dependency())
        finally:
            AppState.reset_standalone()
        assert first is second
        assert type(first).__name__ == "ICache"

    def test_binds_provides_factory_returning_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # The marker declares intent, so a factory may annotate a concrete
        # *implementation* of the interface; it is matched by the return type's
        # direct interface bases, exactly like an implementation class. The
        # returned class is abstract here, so it is not itself a bare candidate.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    "import abc\n"
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "class RedisCache(ICache, abc.ABC):\n"
                    "    @abc.abstractmethod\n"
                    "    def ping(self) -> None: ...\n"
                ),
                "infra/factory.py": (
                    "from fastapi_standalone_di import provides\n"
                    f"from {_ROOT}.infra.redis import RedisCache\n\n\n"
                    "@provides\n"
                    "def build_cache() -> RedisCache:\n"
                    "    raise NotImplementedError\n"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        build_cache = _cls(root, "infra.factory", "build_cache")
        assert result == [Binding(icache, build_cache, False)]

    @pytest.mark.usefixtures("_unpatch_depends")
    def test_binds_provides_factory_with_depends_param_under_patch(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A @provides factory whose parameter is ``Annotated[Port, Depends(Port)]``
        # must still be classified by its *return* type alone. Resolving the whole
        # signature would build the parameter's ``Depends(Port)`` metadata, and
        # under the patch that eagerly dereferences ``Port`` — unbound mid-sweep —
        # which used to make the factory look as if it had no resolvable return
        # type. Order no longer matters: the patch is active before the sweep here.
        root = make_package(
            {
                "contracts/store.py": _iface("IStore"),
                "infra/factory.py": (
                    "from __future__ import annotations\n"
                    "from typing import Annotated\n"
                    "from fastapi import Depends\n"
                    "from fastapi_standalone_di import provides\n"
                    f"from {_ROOT}.contracts.store import IStore\n\n\n"
                    "@provides(primary=True)\n"
                    "def build_store(dep: Annotated[IStore, Depends(IStore)]) "
                    "-> IStore:\n"
                    "    return IStore()\n"
                ),
            }
        )
        patch_for_registrable_dependency_support()
        result = auto_bindings(root)
        istore = _cls(root, "contracts.store", "IStore")
        build_store = _cls(root, "infra.factory", "build_store")
        assert result == [Binding(istore, build_store, False)]

    def test_provides_unresolvable_return_forward_ref_is_reported(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # Under ``from __future__ import annotations`` the return type is a string.
        # When it names something the factory's module cannot resolve, evaluating
        # it fails and the factory carries no return class — a reported misuse.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": (
                    "from __future__ import annotations\n"
                    "from fastapi_standalone_di import provides\n\n\n"
                    "@provides\n"
                    "def build() -> Missing:\n"
                    "    raise NotImplementedError\n"
                ),
            }
        )
        with pytest.raises(AutoBindingError) as excinfo:
            auto_bindings(root)
        message = str(excinfo.value)
        assert "build" in message
        assert "@provides has no resolvable return type" in message

    def test_provides_without_return_type_is_reported(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # @provides promises an implementation, so a missing return annotation
        # carries no interface to match and is a reported misuse — not ignored.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": (
                    "from fastapi_standalone_di import provides\n\n\n"
                    "@provides\n"
                    "def build():\n"
                    "    return object()\n"
                ),
            }
        )
        with pytest.raises(AutoBindingError) as excinfo:
            auto_bindings(root)
        message = str(excinfo.value)
        assert "build" in message
        assert "@provides has no resolvable return type" in message

    def test_provides_returning_unrelated_type_is_reported(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A @provides returning a type unrelated to RegistrableDependency cannot
        # implement any interface — reported, since the marker promised one.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": (
                    "from fastapi_standalone_di import provides\n\n\n"
                    "@provides\n"
                    "def build() -> int:\n"
                    "    return 0\n"
                ),
            }
        )
        with pytest.raises(AutoBindingError) as excinfo:
            auto_bindings(root)
        message = str(excinfo.value)
        assert "build" in message
        assert "not a RegistrableDependency interface" in message

    def test_undecorated_factory_function_is_ignored(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # @provides is mandatory on a factory function: a plain function returning
        # an interface is not one and is left alone, so the interface is unmatched.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": (
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "def build() -> ICache:\n"
                    "    return ICache()\n"
                ),
            }
        )
        with pytest.raises(AutoBindingError, match="no matching implementation"):
            auto_bindings(root)

    def test_provides_on_class_is_optional_noop(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # @provides is optional on a class: it is wired by its hierarchy as usual,
        # the marker being ignored (it will carry future options like primary=).
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    "from fastapi_standalone_di import provides\n"
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "@provides\n"
                    "class RedisCache(ICache): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        assert result == [Binding(icache, icache.impl, False)]
        assert icache.impl.__name__ == "RedisCache"
        assert isinstance(icache.impl, type)

    def test_provides_on_singleton_class_is_noop(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # @singleton on a class yields a function wrapper (not a type) that unwraps
        # to the class; @provides on top of it must stay a no-op — the class is
        # wired by its hierarchy, not treated as a (return-less) factory function.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    "from fastapi_standalone_di import provides, singleton\n"
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "@provides\n"
                    "@singleton\n"
                    "class RedisCache(ICache):\n"
                    "    def __init__(self) -> None: ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        assert result == [Binding(icache, icache.impl, False)]
        assert icache.impl.__name__ == "RedisCache"
        assert not isinstance(icache.impl, type)  # the singleton wrapper

    def test_provides_factory_wins_over_unmarked_class(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A @provides candidate is marked; a bare class is not. Marked beats
        # unmarked, so the factory wins the tie with no conflict_solver.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/mem.py": _impl("MemCache", "ICache", "contracts.cache"),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        build_cache = _cls(root, "infra.factory", "build_cache")
        assert result == [Binding(icache, build_cache, False)]

    def test_provides_class_wins_over_unmarked_class(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # Marked-over-unmarked applies to a class too: a @provides class beats a
        # bare implementation class, no primary or conflict_solver needed.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _provides_class(
                    "RedisCache", "ICache", "contracts.cache"
                ),
                "infra/mem.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )
        auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        assert icache.impl.__name__ == "RedisCache"

    def test_conflict_solver_only_sees_marked_when_marked_present(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # With several marked candidates and some unmarked ones, only the marked
        # contenders remain tied — the unmarked classes are already deprioritised,
        # so the conflict_solver never sees them.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _provides_factory(
                    "build_redis", "ICache", "contracts.cache"
                ),
                "infra/mem.py": _provides_factory(
                    "build_mem", "ICache", "contracts.cache"
                ),
                "infra/plain.py": _impl("PlainCache", "ICache", "contracts.cache"),
            }
        )
        seen: list[Callable[..., object]] = []

        def solver(
            interface: type[RegistrableDependency],
            impls: list[Callable[..., object]],
        ) -> Callable[..., object] | None:
            seen.extend(impls)
            return next(i for i in impls if i.__name__ == "build_redis")

        result = auto_bindings(root, conflict_solver=solver)
        icache = _cls(root, "contracts.cache", "ICache")
        assert {i.__name__ for i in seen} == {"build_redis", "build_mem"}
        assert result == [Binding(icache, icache.impl, False)]
        assert icache.impl.__name__ == "build_redis"

    @pytest.mark.parametrize("gen", ["async", "sync"])
    def test_binds_provides_generator_factory_by_element(
        self, make_package: Callable[[dict[str, str]], str], gen: str
    ) -> None:
        # A @provides generator factory with yield teardown annotates its return
        # as AsyncIterator[Interface] / Iterator[Interface]; the yielded element
        # is the interface it provides, so the layer is unwrapped to match it.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache", gen=gen
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        build_cache = _cls(root, "infra.factory", "build_cache")
        assert result == [Binding(icache, build_cache, False)]

    def test_binds_provides_generator_returning_implementation(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # The unwrapped element may be a concrete implementation of the interface,
        # matched by its direct interface bases exactly like a class. The returned
        # class is abstract, so it is not itself a bare candidate.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    "import abc\n"
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "class RedisCache(ICache, abc.ABC):\n"
                    "    @abc.abstractmethod\n"
                    "    def ping(self) -> None: ...\n"
                ),
                "infra/factory.py": (
                    "from collections.abc import AsyncIterator\n"
                    "from fastapi_standalone_di import provides\n"
                    f"from {_ROOT}.infra.redis import RedisCache\n\n\n"
                    "@provides\n"
                    "async def build_cache() -> AsyncIterator[RedisCache]:\n"
                    "    raise NotImplementedError\n"
                    "    yield\n"
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        build_cache = _cls(root, "infra.factory", "build_cache")
        assert result == [Binding(icache, build_cache, False)]

    def test_primary_provides_factory_wins_over_class(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # @provides(primary=True) settles the factory-vs-class ambiguity with no
        # conflict_solver: the primary candidate is bound.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/mem.py": _impl("MemCache", "ICache", "contracts.cache"),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache", primary=True
                ),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        build_cache = _cls(root, "infra.factory", "build_cache")
        assert result == [Binding(icache, build_cache, False)]

    def test_primary_class_wins_over_factory(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # primary= applies to a class too, not only a factory function.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/mem.py": _provides_class(
                    "MemCache", "ICache", "contracts.cache", primary=True
                ),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache"
                ),
            }
        )
        auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        assert icache.impl.__name__ == "MemCache"

    def test_primary_class_wins_over_plain_class(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # Two competing implementation classes, one marked primary: it wins with
        # no conflict_solver.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _provides_class(
                    "RedisCache", "ICache", "contracts.cache", primary=True
                ),
                "infra/mem.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(root)
        icache = _cls(root, "contracts.cache", "ICache")
        assert icache.impl.__name__ == "RedisCache"
        assert [b.interface for b in result] == [icache]

    def test_primary_takes_precedence_over_conflict_solver(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A single primary is honoured before the conflict_solver is consulted, so
        # the solver never runs for that interface.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _provides_class(
                    "RedisCache", "ICache", "contracts.cache", primary=True
                ),
                "infra/mem.py": _impl("MemCache", "ICache", "contracts.cache"),
            }
        )
        calls: list[type] = []

        def solver(
            interface: type[RegistrableDependency], impls: list[type]
        ) -> type | None:
            calls.append(interface)
            return next(i for i in impls if i.__name__ == "MemCache")

        result = auto_bindings(root, conflict_solver=solver)
        icache = _cls(root, "contracts.cache", "ICache")
        assert calls == []
        assert icache.impl.__name__ == "RedisCache"
        assert [b.interface for b in result] == [icache]

    def test_several_primaries_for_one_interface_is_error(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # Two candidates both marked primary cannot elect a winner — a reported
        # misconfiguration, distinct from a plain ambiguity.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _provides_class(
                    "RedisCache", "ICache", "contracts.cache", primary=True
                ),
                "infra/mem.py": _provides_class(
                    "MemCache", "ICache", "contracts.cache", primary=True
                ),
            }
        )
        with pytest.raises(AutoBindingError, match="primary=True") as excinfo:
            auto_bindings(root)
        message = str(excinfo.value)
        assert "RedisCache" in message
        assert "MemCache" in message

    def test_primary_on_singleton_generator_factory_wins(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # The real teardown case: two @singleton(lazy=True) @provides generator
        # factories for one interface, one primary. The marker is read through the
        # singleton wrapper, so the primary factory wins and its wrapper is bound.
        root = make_package(
            {
                "contracts/store.py": _iface("SeenStore"),
                "infra/redis.py": _provides_factory(
                    "build_redis_store",
                    "SeenStore",
                    "contracts.store",
                    singleton=True,
                    lazy=True,
                    primary=True,
                    gen="async",
                ),
                "infra/memory.py": _provides_factory(
                    "build_memory_store",
                    "SeenStore",
                    "contracts.store",
                    singleton=True,
                    lazy=True,
                    gen="async",
                ),
            }
        )
        result = auto_bindings(root)
        store = _cls(root, "contracts.store", "SeenStore")
        build_redis_store = _cls(root, "infra.redis", "build_redis_store")
        assert result == [Binding(store, build_redis_store, False)]

    def test_primary_marker_is_not_inherited(self) -> None:
        # primary is read from a class's own __dict__, so a subclass does not
        # inherit its parent's primary flag.
        from fastapi_standalone_di import provides
        from fastapi_standalone_di.discovery import _is_primary

        @provides(primary=True)
        class Base: ...

        class Sub(Base): ...

        assert _is_primary(Base) is True
        assert _is_primary(Sub) is False

    def test_bare_provides_is_not_primary(self) -> None:
        # A bare @provides carries primary=False, so it does not silently win a tie.
        from fastapi_standalone_di import provides
        from fastapi_standalone_di.discovery import _is_primary

        @provides
        def build() -> None: ...

        assert _is_primary(build) is False


class TestAstPrefilter:
    """The default ``ast=True`` static pre-filter: only modules that can hold an
    interface or an implementation are imported; everything else is left alone,
    so its import-time side effects never run."""

    def test_skips_modules_without_di_classes(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A module that neither declares an interface nor an implementation — a
        # router, a settings module, anything — is never imported. The trap
        # would raise on import; the wiring still succeeds because it is skipped.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "routers/users.py": _trap(),
            }
        )
        result = auto_bindings(root)
        assert [b.interface.__name__ for b in result] == ["ICache"]
        assert result[0].implementation.__name__ == "RedisCache"

    def test_imports_provides_factory_module_without_class(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A module holding only a ``@provides`` factory declares no class, so the
        # marker decorator is what keeps it: it is imported and wired, while an
        # unrelated trap module is still skipped.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache"
                ),
                "routers/users.py": _trap(),
            }
        )
        result = auto_bindings(root)
        assert [b.interface.__name__ for b in result] == ["ICache"]
        assert result[0].implementation.__name__ == "build_cache"

    def test_imports_aliased_provides_factory_module(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # The @provides decorator is recognised through an ``as`` alias, so the
        # factory module is still imported by the pre-filter.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/factory.py": (
                    "from fastapi_standalone_di import provides as prov\n"
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "@prov\n"
                    "def build_cache() -> ICache:\n"
                    "    return ICache()\n"
                ),
                "routers/users.py": _trap(),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "build_cache"

    def test_skips_interface_consumer_that_does_not_subclass(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # The headline case: a module that *uses* an interface (imports it, e.g.
        # for Depends) without subclassing it is neither an interface nor an
        # implementation, so it stays unimported.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "routers/users.py": (
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "consumer = ICache\n"
                    "raise RuntimeError('module must not be imported')\n"
                ),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "RedisCache"

    def test_disabling_prefilter_imports_every_module(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # With ast=False every module is imported to be inspected, so a module
        # with import-time side effects runs — the very behaviour the prefilter
        # avoids.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "routers/users.py": _trap(),
            }
        )
        with pytest.raises(RuntimeError, match="must not be imported"):
            auto_bindings(root, ast=False)

    def test_resolves_aliased_marker_base(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # An interface declared against an aliased RegistrableDependency import
        # is still recognised: the alias is resolved back to the original name.
        root = make_package(
            {
                "contracts/cache.py": (
                    "from fastapi_standalone_di import (\n"
                    "    RegistrableDependency as RD,\n"
                    ")\n\n\n"
                    "class ICache(RD): ...\n"
                ),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(root)
        assert [b.interface.__name__ for b in result] == ["ICache"]
        assert result[0].implementation.__name__ == "RedisCache"

    def test_resolves_aliased_interface_base(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    f"from {_ROOT}.contracts.cache import ICache as Cache\n\n\n"
                    "class RedisCache(Cache): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "RedisCache"

    def test_resolves_attribute_interface_base(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    f"from {_ROOT}.contracts import cache\n\n\n"
                    "class RedisCache(cache.ICache): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "RedisCache"

    def test_imports_dynamic_base_conservatively(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A base class the analysis cannot resolve statically (here a call) is
        # never a reason to skip: the module is imported so a real subclass is
        # never lost.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "def _base() -> type:\n"
                    "    return ICache\n\n\n"
                    "class RedisCache(_base()): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "RedisCache"

    def test_imports_dynamic_marker_base_in_interface_only_scope(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # An interface whose *marker* base is computed dynamically is invisible
        # to the static analysis. Even in an interface-only scope (pass 2 never
        # runs), pass 1 imports it anyway, so the interface is still discovered.
        root = make_package(
            {
                "contracts/cache.py": (
                    "from fastapi_standalone_di import RegistrableDependency\n\n\n"
                    "def _marker() -> type:\n"
                    "    return RegistrableDependency\n\n\n"
                    "class ICache(_marker()): ...\n"
                ),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        result = auto_bindings(
            interfaces=[f"{root}.contracts"], implementations=[f"{root}.infra"]
        )
        assert [b.interface.__name__ for b in result] == ["ICache"]
        assert result[0].implementation.__name__ == "RedisCache"

    def test_resolves_generic_base_without_mismatch(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A subscripted generic base resolves to its bare name and never hides
        # the real interface base declared alongside it.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    "from typing import Generic, TypeVar\n"
                    f"from {_ROOT}.contracts.cache import ICache\n\n\n"
                    "T = TypeVar('T')\n\n\n"
                    "class RedisCache(ICache, Generic[T]): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "RedisCache"

    def test_star_imported_interface_base_is_imported_conservatively(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A star import hides where a base name comes from; the module is imported
        # conservatively so an implementation behind it is never lost.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": (
                    f"from {_ROOT}.contracts.cache import *\n\n\n"
                    "class RedisCache(ICache): ...\n"
                ),
            }
        )
        result = auto_bindings(root)
        assert result[0].implementation.__name__ == "RedisCache"

    def test_unparseable_module_is_imported_and_surfaces(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A module whose source cannot be parsed is imported rather than skipped
        # — a real defect surfaces here instead of being silently ignored.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
                "broken/oops.py": "def (:\n",
            }
        )
        with pytest.raises(SyntaxError):
            auto_bindings(root)

    def test_module_root_without_submodules(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # Roots may be plain modules (no __path__): they are inspected directly,
        # with nothing to enumerate underneath, in both scan modes.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        for ast_enabled in (True, False):
            AppState.reset_standalone()
            _cls(root, "contracts.cache", "ICache").register(None)
            result = auto_bindings(
                interfaces=[f"{root}.contracts.cache"],
                implementations=[f"{root}.infra.redis"],
                ast=ast_enabled,
            )
            assert result[0].implementation.__name__ == "RedisCache"

    def test_exhaustive_walk_binds_across_nesting_and_overlap(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # ast=False keeps the exhaustive import walk: nested implementations are
        # found, and an overlapping root is visited only once.
        root = make_package(
            {
                "contracts/__init__.py": _iface("ICache"),
                "infra/deep/redis.py": _impl("RedisCache", "ICache", "contracts"),
            }
        )
        overlapping = auto_bindings(root, interfaces=[f"{root}.contracts"], ast=False)
        assert overlapping[0].implementation.__name__ == "RedisCache"

        # Reverse order: the shared subtree is visited under the first root, so
        # the second root's walk skips the already-seen submodules.
        AppState.reset_standalone()
        _cls(root, "contracts", "ICache").register(None)
        reversed_ = auto_bindings(f"{root}.contracts", root, ast=False)
        assert reversed_[0].implementation.__name__ == "RedisCache"

    def test_exhaustive_walk_non_recursive_stops_at_top_level(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # ast=False, recursive=False: the top-level interface is found but the
        # nested implementation is not walked, so wiring fails.
        root = make_package(
            {
                "contracts/__init__.py": _iface("ICache"),
                "infra/deep/redis.py": _impl("RedisCache", "ICache", "contracts"),
            }
        )
        with pytest.raises(AutoBindingError, match="no matching"):
            auto_bindings(root, ast=False, recursive=False)


def test_read_source_returns_none_for_missing_or_unreadable(tmp_path: Path) -> None:
    # A missing path and an unreadable one (here a directory) both yield None, so
    # the caller imports the module rather than reasoning from absent source.
    from fastapi_standalone_di.discovery import _read_source

    assert _read_source(None) is None
    assert _read_source(str(tmp_path)) is None


def test_analyze_module_returns_none_without_source() -> None:
    # Unreadable source (None) yields no analysis, so the caller imports the
    # module rather than reasoning from absent source.
    from fastapi_standalone_di.discovery import _analyze_module

    assert _analyze_module(None) is None


_CACHED_TREE = {
    "contracts/__init__.py": "",
    "contracts/cache.py": _iface("ICache", "IStore"),
    "contracts/aliased.py": (
        "from fastapi_standalone_di import RegistrableDependency as RD\n\n\n"
        "class IClock(RD): ...\n"
    ),
    "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
    "infra/store.py": (
        f"from {_ROOT}.contracts.cache import *\n\n\nclass SqlStore(IStore): ...\n"
    ),
    "infra/clock.py": _provides_factory("build_clock", "IClock", "contracts.aliased"),
    "infra/dynamic.py": (
        "def _base() -> type:\n    return object\n\n\nclass Thing(_base()): ...\n"
    ),
    "routers/users.py": (
        f"from {_ROOT}.contracts.cache import ICache\n\n\n"
        "consumer = ICache\n"
        "raise RuntimeError('module must not be imported')\n"
    ),
    "settings.py": "DEBUG = True\n",
}

# make_package adds the __init__.py of infra/ and routers/.
_CACHED_TREE_MODULES = len(_CACHED_TREE) + 2


def _select(root: str, cache_file: Path | None) -> list[str]:
    from fastapi_standalone_di.discovery import _select_modules_ast

    modules = _select_modules_ast(
        [importlib.import_module(root)],
        recursive=True,
        interface_names={root},
        implementation_names={root},
        cache_file=cache_file,
    )
    return [module.__name__ for module in modules]


def _cached_modules(cache_file: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(cache_file.read_bytes())["modules"])


@pytest.fixture
def parse_calls(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Every source the pre-filter hands to ``ast.parse``."""
    calls: list[bytes] = []
    real_parse = ast.parse

    def spy(source: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(source)
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(discovery.ast, "parse", spy)
    return calls


@pytest.fixture
def replace_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(discovery.os, "replace", spy)
    return calls


class TestAstCache:
    """``cache_file``: the pre-filter's per-module analysis is persisted, keyed by
    the source checksum, and only modules whose source changed are parsed again."""

    @pytest.fixture(autouse=True)
    def _fresh_fingerprint(self) -> Iterator[None]:
        # Memoized for the process: a test that patches what it hashes must not
        # leave its fingerprint to the next one.
        yield
        discovery._analyzer_fingerprint.cache_clear()

    def test_cached_and_uncached_select_the_same_modules(
        self, make_package: Callable[[dict[str, str]], str], tmp_path: Path
    ) -> None:
        root = make_package(_CACHED_TREE)
        cache_file = tmp_path / "cache" / "bindings.json"
        uncached = _select(root, None)
        assert f"{root}.routers.users" not in uncached
        assert _select(root, cache_file) == uncached
        assert cache_file.exists()
        assert _select(root, cache_file) == uncached

    def test_cached_and_uncached_produce_the_same_bindings(
        self, make_package: Callable[[dict[str, str]], str], tmp_path: Path
    ) -> None:
        root = make_package(_CACHED_TREE)
        cache_file = tmp_path / "bindings.json"
        interfaces = [
            _cls(root, "contracts.cache", "ICache"),
            _cls(root, "contracts.cache", "IStore"),
            _cls(root, "contracts.aliased", "IClock"),
        ]
        results = []
        for cache in (None, cache_file, cache_file):
            AppState.reset_standalone()
            for interface in interfaces:
                interface.register(None)
            results.append(auto_bindings(root, cache_file=cache))
        assert results[0] == results[1] == results[2]
        assert [b.implementation.__name__ for b in results[0]] == [
            "build_clock",
            "RedisCache",
            "SqlStore",
        ]

    @pytest.mark.parametrize(
        "source",
        [
            _iface("ICache"),
            _impl("RedisCache", "ICache", "contracts.cache"),
            _provides_factory("build_cache", "ICache", "contracts.cache"),
            f"from {_ROOT}.contracts.cache import *\n\n\nclass S(IStore): ...\n",
            "class Thing(make_base()): ...\n",
            "def (:\n",
            "",
        ],
        ids=["interface", "impl", "provider", "star", "dynamic", "syntax", "empty"],
    )
    def test_round_trips_the_analysis(self, source: str, tmp_path: Path) -> None:
        cache_file = tmp_path / "bindings.json"
        inventory: dict[str, bytes | None] = {"pkg.mod": source.encode()}
        expected = discovery._analyze_module(source.encode())
        cold = discovery._analyze_cached(
            inventory, cache_file, root_names=["pkg"], recursive=True
        )
        warm = discovery._analyze_cached(
            inventory, cache_file, root_names=["pkg"], recursive=True
        )
        assert cold == warm == {"pkg.mod": expected}
        stored = discovery._read_analysis_cache(cache_file)["pkg.mod"].analysis
        assert stored == expected
        assert type(stored) is type(expected)
        if stored is not None and expected is not None:
            assert isinstance(stored.base_names, frozenset)

    def test_warm_cache_parses_nothing_and_writes_nothing(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        parse_calls: list[bytes],
        replace_calls: list[tuple[Path, Path]],
    ) -> None:
        root = make_package(_CACHED_TREE)
        cache_file = tmp_path / "bindings.json"
        _select(root, cache_file)
        assert len(parse_calls) == _CACHED_TREE_MODULES
        assert len(replace_calls) == 1

        parse_calls.clear()
        replace_calls.clear()
        _select(root, cache_file)
        assert parse_calls == []
        assert replace_calls == []

    def test_changed_and_new_modules_are_reanalysed(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        parse_calls: list[bytes],
    ) -> None:
        # The changed module was skipped by the first sweep, so only a fresh
        # analysis of its new source can get it imported and wired.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/memory.py": _impl("MemoryCache", "ICache", "contracts.cache"),
                "infra/redis.py": "VALUE = 1\n",
                "infra/gone.py": "VALUE = 2\n",
            }
        )
        cache_file = tmp_path / "bindings.json"
        assert auto_bindings(root, cache_file=cache_file)[
            0
        ].implementation.__name__ == ("MemoryCache")

        infra = tmp_path / root / "infra"
        redis = _provides_class("RedisCache", "ICache", "contracts.cache", primary=True)
        (infra / "redis.py").write_text(redis)
        (infra / "extra.py").write_text("VALUE = 3\n")
        (infra / "gone.py").unlink()
        parse_calls.clear()
        AppState.reset_standalone()
        _cls(root, "contracts.cache", "ICache").register(None)

        result = auto_bindings(root, cache_file=cache_file)
        assert result[0].implementation.__name__ == "RedisCache"
        assert sorted(parse_calls) == sorted([redis.encode(), b"VALUE = 3\n"])
        modules = _cached_modules(cache_file)
        assert f"{root}.infra.extra" in modules
        assert f"{root}.infra.gone" not in modules

    def test_keeps_the_entries_of_packages_another_call_sweeps(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        parse_calls: list[bytes],
    ) -> None:
        root = make_package(
            {
                "a/cache.py": _iface("ICache"),
                "a/redis.py": _impl("RedisCache", "ICache", "a.cache"),
                "b/clock.py": _iface("IClock"),
                "b/system.py": _impl("SystemClock", "IClock", "b.clock"),
            }
        )
        cache_file = tmp_path / "bindings.json"
        auto_bindings(f"{root}.a", cache_file=cache_file)
        auto_bindings(f"{root}.b", cache_file=cache_file)
        assert set(_cached_modules(cache_file)) == {
            f"{root}.a.cache",
            f"{root}.a.redis",
            f"{root}.b.clock",
            f"{root}.b.system",
        }

        parse_calls.clear()
        AppState.reset_standalone()
        _cls(root, "a.cache", "ICache").register(None)
        auto_bindings(f"{root}.a", cache_file=cache_file)
        assert parse_calls == []

    def test_module_without_source_is_not_stored(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "bindings.json"
        analyses = discovery._analyze_cached(
            {"pkg.compiled": None, "pkg.plain": b""},
            cache_file,
            root_names=["pkg"],
            recursive=True,
        )
        assert analyses["pkg.compiled"] is None
        assert set(_cached_modules(cache_file)) == {"pkg.plain"}

    def test_non_recursive_sweep_keeps_nested_entries(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "bindings.json"
        inventory: dict[str, bytes | None] = {
            "pkg.top": b"",
            "pkg.sub": b"",
            "pkg.sub.deep": b"",
        }
        discovery._analyze_cached(
            inventory, cache_file, root_names=["pkg"], recursive=True
        )
        discovery._analyze_cached(
            {"pkg.top": b""}, cache_file, root_names=["pkg"], recursive=False
        )
        assert set(_cached_modules(cache_file)) == {"pkg.top", "pkg.sub.deep"}

    @pytest.mark.parametrize(
        "content",
        [
            b"{not json",
            b"\xff\xfe",
            b"[]",
            b'{"format": 999, "analyzer": "x", "modules": {}}',
            b'{"format": 1, "analyzer": "%s", "modules": []}',
            b'{"format": 1, "analyzer": "%s", "modules": {"m": {"checksum": 1}}}',
            b'{"format": 1, "analyzer": "%s", "modules": {"m": {"checksum": "c",'
            b' "analysis": {"defines_marker_subclass": 1}}}}',
            b'{"format": 1, "analyzer": "%s", "modules": {"m": {"checksum": "c",'
            b' "analysis": {"defines_marker_subclass": true, "base_names": "abc",'
            b' "has_provider": false, "has_unresolved_base": false}}}}',
        ],
        ids=[
            "truncated",
            "binary",
            "not-an-object",
            "foreign-format",
            "modules-not-object",
            "bad-checksum",
            "bad-flag",
            "bad-base-names",
        ],
    )
    def test_ignores_and_rewrites_an_invalid_cache(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        content: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = make_package(_CACHED_TREE)
        expected = _select(root, None)
        cache_file = tmp_path / "bindings.json"
        if b"%s" in content:
            content = content % discovery._analyzer_fingerprint().encode()
        cache_file.write_bytes(content)

        with caplog.at_level(logging.DEBUG, logger=discovery.__name__):
            assert _select(root, cache_file) == expected
        assert sum(str(cache_file) in r.getMessage() for r in caplog.records) == 1
        assert len(_cached_modules(cache_file)) == _CACHED_TREE_MODULES

    def test_another_analyzer_invalidates_every_entry(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        parse_calls: list[bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = make_package(_CACHED_TREE)
        cache_file = tmp_path / "bindings.json"
        _select(root, cache_file)

        monkeypatch.setattr(discovery, "_analyzer_fingerprint", lambda: "9.9.9+other")
        parse_calls.clear()
        _select(root, cache_file)
        assert len(parse_calls) == _CACHED_TREE_MODULES
        assert json.loads(cache_file.read_bytes())["analyzer"] == "9.9.9+other"

    def test_fingerprint_carries_the_library_version(self) -> None:
        assert discovery._analyzer_fingerprint().startswith(f"{__version__}+")

    def test_fingerprint_changes_with_the_analysis_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        discovery._analyzer_fingerprint.cache_clear()
        before = discovery._analyzer_fingerprint()
        discovery._analyzer_fingerprint.cache_clear()
        monkeypatch.setattr(
            discovery, "_read_source", lambda _path: b"an edited analysis"
        )
        assert discovery._analyzer_fingerprint() != before

    def test_fingerprint_changes_with_the_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        discovery._analyzer_fingerprint.cache_clear()
        before = discovery._analyzer_fingerprint()
        discovery._analyzer_fingerprint.cache_clear()
        monkeypatch.setattr(sys.implementation, "cache_tag", "another-python")
        assert discovery._analyzer_fingerprint() != before

    def test_fingerprint_survives_an_interpreter_with_no_cache_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        discovery._analyzer_fingerprint.cache_clear()
        monkeypatch.setattr(sys.implementation, "cache_tag", None)
        assert discovery._analyzer_fingerprint().startswith(f"{__version__}+")

    def test_an_empty_path_means_no_cache(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unset environment variable passed straight through reads as ``""``."""
        root = make_package(_CACHED_TREE)
        monkeypatch.chdir(tmp_path)
        written: list[Path] = []
        monkeypatch.setattr(
            discovery,
            "_write_analysis_cache",
            lambda path, _entries: written.append(path),
        )
        auto_bindings(root, cache_file="")
        assert written == []

    @pytest.mark.parametrize("path", [".", "/"])
    def test_a_path_naming_no_file_does_not_raise(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
    ) -> None:
        root = make_package(_CACHED_TREE)
        monkeypatch.chdir(tmp_path)
        assert _select(root, Path(path)) == _select(root, None)

    def test_creates_missing_parent_directories_from_a_str_path(
        self, make_package: Callable[[dict[str, str]], str], tmp_path: Path
    ) -> None:
        root = make_package(_CACHED_TREE)
        cache_file = tmp_path / "a" / "b" / "bindings.json"
        auto_bindings(root, cache_file=str(cache_file))
        assert len(_cached_modules(cache_file)) == _CACHED_TREE_MODULES

    def test_unwritable_location_does_not_raise(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = make_package(_CACHED_TREE)
        expected = _select(root, None)
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        cache_file = blocker / "bindings.json"

        with caplog.at_level(logging.INFO, logger=discovery.__name__):
            assert _select(root, cache_file) == expected
        assert "Cannot write" in caplog.text

    @pytest.mark.skipif(
        not hasattr(os, "geteuid") or os.geteuid() == 0,
        reason="permission bits do not bind root",
    )
    def test_read_only_directory_does_not_raise(
        self, make_package: Callable[[dict[str, str]], str], tmp_path: Path
    ) -> None:
        root = make_package(_CACHED_TREE)
        expected = _select(root, None)
        read_only = tmp_path / "read-only"
        read_only.mkdir()
        read_only.chmod(0o500)
        try:
            assert _select(root, read_only / "bindings.json") == expected
            assert list(read_only.iterdir()) == []
        finally:
            read_only.chmod(0o700)

    def test_unreadable_cache_is_ignored(
        self, make_package: Callable[[dict[str, str]], str], tmp_path: Path
    ) -> None:
        root = make_package(_CACHED_TREE)
        expected = _select(root, None)
        cache_file = tmp_path / "bindings.json"
        cache_file.mkdir()
        assert _select(root, cache_file) == expected

    def test_writes_through_a_sibling_temporary_file(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = make_package(_CACHED_TREE)
        cache_file = tmp_path / "cache" / "bindings.json"
        swapped: list[Path] = []
        real_replace = os.replace

        def checking_replace(src: Any, dst: Any) -> None:
            # Whatever a concurrent reader opens is either the previous file or
            # this complete one: the payload is whole before it is swapped in.
            assert Path(dst) == cache_file
            assert Path(src).parent == cache_file.parent
            assert json.loads(Path(src).read_bytes())["modules"]
            swapped.append(Path(src))
            real_replace(src, dst)

        monkeypatch.setattr(discovery.os, "replace", checking_replace)
        _select(root, cache_file)
        assert len(swapped) == 1
        assert sorted(cache_file.parent.iterdir()) == [cache_file]

    @pytest.mark.parametrize("error", [OSError("disk full"), KeyboardInterrupt()])
    def test_a_failed_write_leaves_the_previous_cache_whole(
        self,
        make_package: Callable[[dict[str, str]], str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error: BaseException,
    ) -> None:
        root = make_package(_CACHED_TREE)
        cache_file = tmp_path / "cache" / "bindings.json"
        _select(root, cache_file)
        previous = cache_file.read_bytes()
        (tmp_path / root / "settings.py").write_text("DEBUG = False\n")

        real_fdopen = os.fdopen

        def failing_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
            handle = real_fdopen(fd, *args, **kwargs)
            real_write = handle.write

            def write(data: bytes) -> int:
                real_write(data[: len(data) // 2])
                raise error

            handle.write = write
            return handle

        monkeypatch.setattr(discovery.os, "fdopen", failing_fdopen)
        if isinstance(error, OSError):
            _select(root, cache_file)
        else:
            with pytest.raises(KeyboardInterrupt):
                _select(root, cache_file)
        assert cache_file.read_bytes() == previous
        assert sorted(cache_file.parent.iterdir()) == [cache_file]

    def test_ignored_without_the_prefilter(
        self, make_package: Callable[[dict[str, str]], str], tmp_path: Path
    ) -> None:
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _impl("RedisCache", "ICache", "contracts.cache"),
            }
        )
        cache_file = tmp_path / "bindings.json"
        auto_bindings(root, ast=False, cache_file=cache_file)
        assert not cache_file.exists()
