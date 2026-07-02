"""Tests for fastapi_standalone_di.resolve."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import Depends

from fastapi_standalone_di import (
    DependantCache,
    FastAPIContainer,
    RegistrableDependency,
    ResolvedDependencies,
)

# ---------------------------------------------------------------------------
# Test fixtures: fake dependency graph
# ---------------------------------------------------------------------------


class ILeafDep(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class LeafDep(ILeafDep):
    def value(self) -> str:
        return "leaf"


class IMiddleDep(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class MiddleDep(IMiddleDep):
    def __init__(self, leaf: ILeafDep = Depends(ILeafDep)) -> None:
        self.leaf = leaf

    def value(self) -> str:
        return f"middle({self.leaf.value()})"


class IRootDep(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class RootDep(IRootDep):
    def __init__(self, middle: IMiddleDep = Depends(IMiddleDep)) -> None:
        self.middle = middle

    def value(self) -> str:
        return f"root({self.middle.value()})"


# --- yield-based dependency ------------------------------------------------

_yield_cleanup_called = False


class IYieldDep(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class YieldDepImpl(IYieldDep):
    def value(self) -> str:
        return "yielded"


async def _yield_dep_factory() -> AsyncIterator[YieldDepImpl]:
    global _yield_cleanup_called
    _yield_cleanup_called = False
    yield YieldDepImpl()
    _yield_cleanup_called = True


# --- sync yield-based dependency ------------------------------------------

_sync_yield_cleanup_called = False


def _sync_yield_dep_factory() -> Iterator[str]:
    global _sync_yield_cleanup_called
    _sync_yield_cleanup_called = False
    yield "sync-yielded"
    _sync_yield_cleanup_called = True


# --- plain function dependency (non-class) ---------------------------------


def plain_sync_dep() -> str:
    return "sync-plain"


async def plain_async_dep() -> str:
    return "async-plain"


# ---------------------------------------------------------------------------
# Register implementations for the test session
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_deps() -> Iterator[None]:
    ILeafDep.register(LeafDep)
    IMiddleDep.register(MiddleDep)
    IRootDep.register(RootDep)
    IYieldDep.register(_yield_dep_factory)
    yield
    ILeafDep.register(None)
    IMiddleDep.register(None)
    IRootDep.register(None)
    IYieldDep.register(None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

container = FastAPIContainer()


class TestResolve:
    async def test_simple_resolution(self) -> None:
        leaf = await container.get(ILeafDep)
        assert leaf.value() == "leaf"

    async def test_recursive_resolution(self) -> None:
        root = await container.get(IRootDep)
        assert root.value() == "root(middle(leaf))"

    async def test_multiple_dependencies(self) -> None:
        deps = await container.resolve(ILeafDep, IMiddleDep)
        leaf = deps.get(ILeafDep)
        middle = deps.get(IMiddleDep)
        assert leaf.value() == "leaf"
        assert middle.value() == "middle(leaf)"

    async def test_yield_dependency(self) -> None:
        dep = await container.get(IYieldDep)
        assert dep.value() == "yielded"

    async def test_get_unresolved_raises_key_error(self) -> None:
        deps = await container.resolve(ILeafDep)
        with pytest.raises(KeyError, match="was not resolved"):
            deps.get(IRootDep)

    async def test_optional_unresolved(self) -> None:
        deps = await container.resolve(ILeafDep)
        assert deps.optional(IRootDep) is None

    async def test_optional_resolved(self) -> None:
        deps = await container.resolve(ILeafDep)
        assert deps.optional(ILeafDep) is not None

    async def test_plain_sync_function_dependency(self) -> None:
        result = await container.get(plain_sync_dep)
        assert result == "sync-plain"

    async def test_plain_async_function_dependency(self) -> None:
        result: str = await container.get(plain_async_dep)  # type: ignore[arg-type]
        assert result == "async-plain"

    async def test_sync_generator_dependency(self) -> None:
        result: str = await container.get(_sync_yield_dep_factory)  # type: ignore[arg-type]
        assert result == "sync-yielded"

    async def test_no_dependencies(self) -> None:
        deps = await container.resolve()
        assert isinstance(deps, ResolvedDependencies)


class TestDependantCache:
    async def test_cache_false_no_caching(self) -> None:
        """dependant_cache=False disables introspection caching."""
        c = FastAPIContainer(dependant_cache=False)
        leaf = await c.get(ILeafDep)
        assert leaf.value() == "leaf"

    async def test_cache_true_scoped_to_call(self) -> None:
        """dependant_cache=True uses a temporary cache for one call."""
        c = FastAPIContainer(dependant_cache=True)
        root = await c.get(IRootDep)
        assert root.value() == "root(middle(leaf))"

    async def test_shared_cache_across_calls(self) -> None:
        """A DependantCache instance can be shared across multiple resolve() calls."""
        cache = DependantCache()
        c = FastAPIContainer(dependant_cache=cache)

        leaf = await c.get(ILeafDep)
        assert leaf.value() == "leaf"

        root = await c.get(IRootDep)
        assert root.value() == "root(middle(leaf))"

        assert cache.get_dependant(LeafDep) is not None
        assert cache.get_dependant(MiddleDep) is not None
        assert cache.get_dependant(RootDep) is not None

    async def test_cache_is_populated(self) -> None:
        """Verify the cache stores Dependant objects after resolution."""
        cache = DependantCache()
        assert cache.get_dependant(LeafDep) is None

        c = FastAPIContainer(dependant_cache=cache)
        leaf = await c.get(ILeafDep)
        assert leaf.value() == "leaf"

        assert cache.get_dependant(LeafDep) is not None


class TestContainerGetAndOptional:
    async def test_container_get_returns_instance(self) -> None:
        """container.get() resolves and returns the instance directly."""
        c = FastAPIContainer()
        leaf = await c.get(ILeafDep)
        assert isinstance(leaf, LeafDep)
        assert leaf.value() == "leaf"

    async def test_container_get_resolves_transitive(self) -> None:
        """container.get() resolves the full dependency chain."""
        c = FastAPIContainer()
        root = await c.get(IRootDep)
        assert root.value() == "root(middle(leaf))"

    async def test_container_optional_returns_instance(self) -> None:
        """container.optional() returns the instance when resolvable."""
        c = FastAPIContainer()
        leaf = await c.optional(ILeafDep)
        assert leaf is not None
        assert leaf.value() == "leaf"

    async def test_resolved_deps_get_raises_for_missing(self) -> None:
        """ResolvedDependencies.get() raises KeyError for unresolved deps."""
        deps = await container.resolve(ILeafDep)
        with pytest.raises(KeyError, match="was not resolved"):
            deps.get(IRootDep)

    async def test_resolved_deps_optional_returns_none_for_missing(self) -> None:
        """ResolvedDependencies.optional() returns None for unresolved deps."""
        deps = await container.resolve(ILeafDep)
        assert deps.optional(IRootDep) is None

    async def test_resolved_deps_optional_returns_instance_for_resolved(self) -> None:
        """ResolvedDependencies.optional() returns the instance when resolved."""
        deps = await container.resolve(ILeafDep)
        leaf = deps.optional(ILeafDep)
        assert leaf is not None
        assert leaf.value() == "leaf"


class TestDependencyOverrides:
    async def test_override_replaces_implementation(self) -> None:
        """A dependency override substitutes the original callable."""

        class FakeLeaf(ILeafDep):
            def value(self) -> str:
                return "overridden"

        c = FastAPIContainer(dependency_overrides={LeafDep: FakeLeaf})
        leaf = await c.get(ILeafDep)
        assert leaf.value() == "overridden"

    async def test_override_propagates_to_transitive_deps(self) -> None:
        """Overriding a leaf dependency affects dependents that use it."""

        class FakeLeaf(ILeafDep):
            def value(self) -> str:
                return "fake"

        c = FastAPIContainer(dependency_overrides={LeafDep: FakeLeaf})
        root = await c.get(IRootDep)
        assert root.value() == "root(middle(fake))"

    async def test_no_override_uses_original(self) -> None:
        """Without overrides, the registered implementation is used."""
        c = FastAPIContainer(dependency_overrides={})
        leaf = await c.get(ILeafDep)
        assert leaf.value() == "leaf"


class TestInstanceCache:
    async def test_resolved_instances_are_cached(self) -> None:
        """The same container reuses resolved instances across calls."""
        c = FastAPIContainer()
        leaf1 = await c.get(ILeafDep)
        leaf2 = await c.get(ILeafDep)
        assert leaf1 is leaf2

    async def test_clear_cache_drops_instances(self) -> None:
        """After clear_cache(), instances are re-resolved."""
        c = FastAPIContainer()
        leaf1 = await c.get(ILeafDep)
        c.clear_cache()
        leaf2 = await c.get(ILeafDep)
        assert leaf1 is not leaf2

    async def test_generator_deps_not_cached_across_calls(self) -> None:
        """Generator dependencies are re-created on each resolve() call."""
        c = FastAPIContainer()
        dep1 = await c.get(IYieldDep)
        dep2 = await c.get(IYieldDep)
        assert dep1 is not dep2


class TestTeardown:
    async def test_async_yield_teardown_runs_on_aclose(self) -> None:
        """aclose() runs the teardown of async-generator dependencies."""
        c = FastAPIContainer()
        dep = await c.get(IYieldDep)
        assert dep.value() == "yielded"
        assert _yield_cleanup_called is False
        await c.aclose()
        assert _yield_cleanup_called is True

    async def test_context_manager_runs_teardown(self) -> None:
        """Using the container as an async context manager runs teardown on exit."""
        async with FastAPIContainer() as c:
            result = await c.get(_sync_yield_dep_factory)  # type: ignore[arg-type]
            assert result == "sync-yielded"
            assert _sync_yield_cleanup_called is False
        assert _sync_yield_cleanup_called is True
