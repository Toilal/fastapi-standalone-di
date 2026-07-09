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

    def test_lazy_singleton_decorated_implementation_is_rejected(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A lazy @singleton delegates to container.get(factory), which
        # re-dereferences the implementation class back through the interface it
        # subclasses — a cycle. auto_bindings must reject it, not register a
        # binding that deadlocks at resolution, and register nothing.
        root = make_package(
            {
                "contracts/cache.py": _iface("ICache"),
                "infra/redis.py": _singleton_impl(
                    "RedisCache", "ICache", "contracts.cache", lazy=True
                ),
            }
        )
        icache = _cls(root, "contracts.cache", "ICache")
        with pytest.raises(AutoBindingError, match="lazy @singleton"):
            auto_bindings(root)
        assert icache._impl is None

    def test_unmatched_lazy_singleton_is_ignored_not_rejected(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        # A lazy singleton that is not the implementation of any wired interface is
        # ignored like any other unmatched candidate — only a *selected* lazy
        # implementation is rejected.
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
        assert icache.impl.__name__ == "RedisCache"
