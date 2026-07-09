"""Tests for fastapi_standalone_di.discovery.auto_bindings."""

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fastapi_standalone_di import (
    AppState,
    AutoBindingError,
    Binding,
    FastAPIContainer,
    RegistrableDependency,
    auto_bindings,
)

_ROOT = "_ab_pkg"


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
) -> str:
    """A ``@provides`` factory *function* returning ``returns`` — an interface or a
    concrete implementation of one — optionally also a ``@singleton``."""
    imports = ["from fastapi_standalone_di import provides"]
    decorators = ["@provides"]
    if singleton:
        imports.append("from fastapi_standalone_di import singleton")
        decorators.insert(0, "@singleton(lazy=True)" if lazy else "@singleton")
    imports.append(f"from {_ROOT}.{returns_module} import {returns}")
    body = "\n".join(imports) + "\n\n\n" + "\n".join(decorators) + "\n"
    return f"{body}def {name}() -> {returns}:\n    return {returns}()\n"


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

    def test_factory_and_class_for_same_interface_is_ambiguous(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A @provides factory and a bare class both implementing the same
        # interface is a genuine ambiguity, resolved via conflict_solver.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/mem.py": _impl("MemCache", "ICache", "contracts.cache"),
                "infra/factory.py": _provides_factory(
                    "build_cache", "ICache", "contracts.cache"
                ),
            }
        )
        with pytest.raises(AutoBindingError, match="several matching implementations"):
            auto_bindings(root)

        candidates: list[Callable[..., object]] = []

        def solver(
            interface: type[RegistrableDependency],
            impls: list[Callable[..., object]],
        ) -> Callable[..., object] | None:
            candidates.extend(impls)
            return next(i for i in impls if i.__name__ == "build_cache")

        result = auto_bindings(root, conflict_solver=solver)
        icache = _cls(root, "contracts.cache", "ICache")
        assert {i.__name__ for i in candidates} == {"MemCache", "build_cache"}
        assert result == [Binding(icache, icache.impl, False)]
        assert icache.impl.__name__ == "build_cache"


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
