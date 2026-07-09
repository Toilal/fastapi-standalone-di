"""Tests for fastapi_standalone_di.resolve."""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import BackgroundTasks, Depends, Response
from fastapi.dependencies.utils import get_dependant

from fastapi_standalone_di import (
    AppState,
    CyclicDependencyError,
    DependantCache,
    DependencyScope,
    FastAPIContainer,
    RegistrableDependency,
    ResolvedDependencies,
    get_container,
    singleton,
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


# --- diamond dependency graph: Diamond -> {Left, Right} -> Shared ----------


class IShared(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class SharedDep(IShared):
    def value(self) -> str:
        return "shared"


class ILeftDep(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class LeftDep(ILeftDep):
    def __init__(self, shared: IShared = Depends(IShared)) -> None:
        self.shared = shared

    def value(self) -> str:
        return "left"


class IRightDep(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class RightDep(IRightDep):
    def __init__(self, shared: IShared = Depends(IShared)) -> None:
        self.shared = shared

    def value(self) -> str:
        return "right"


class IDiamondDep(ABC, RegistrableDependency):
    @abstractmethod
    def value(self) -> str: ...


class DiamondDep(IDiamondDep):
    def __init__(
        self,
        left: ILeftDep = Depends(ILeftDep),
        right: IRightDep = Depends(IRightDep),
    ) -> None:
        self.left = left
        self.right = right

    def value(self) -> str:
        return "diamond"


# --- SCOPED yield dependency behind an invoke() entry point ----------------

_session_teardowns: list[object] = []


class Session:
    pass


async def _session_factory() -> AsyncIterator[Session]:
    session = Session()
    yield session
    _session_teardowns.append(session)


def _handler_with_session(session: Session = Depends(_session_factory)) -> str:
    return "handled"


@pytest.fixture(autouse=True)
def _register_deps() -> Iterator[None]:
    ILeafDep.register(LeafDep)
    IMiddleDep.register(MiddleDep)
    IRootDep.register(RootDep)
    IYieldDep.register(_yield_dep_factory)
    IShared.register(SharedDep)
    ILeftDep.register(LeftDep)
    IRightDep.register(RightDep)
    IDiamondDep.register(DiamondDep)
    yield
    ILeafDep.register(None)
    IMiddleDep.register(None)
    IRootDep.register(None)
    IYieldDep.register(None)
    IShared.register(None)
    ILeftDep.register(None)
    IRightDep.register(None)
    IDiamondDep.register(None)


# --- use_cache=False fixtures ---------------------------------------------

_fresh_counter = 0


def fresh_dep() -> int:
    """A dependency that yields a distinct value at every construction."""
    global _fresh_counter
    _fresh_counter += 1
    return _fresh_counter


def consumer_a(value: int = Depends(fresh_dep, use_cache=False)) -> int:
    return value


def consumer_b(value: int = Depends(fresh_dep, use_cache=False)) -> int:
    return value


def root_uncached(
    a: int = Depends(consumer_a),
    b: int = Depends(consumer_b),
) -> tuple[int, int]:
    return (a, b)


def handler_with_deps(root: IRootDep = Depends(IRootDep)) -> str:
    return root.value()


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

    def test_keyed_by_callable(self) -> None:
        """The cache is keyed by the callable itself, not by its id()."""

        def call() -> None: ...

        cache = DependantCache()
        dependant = get_dependant(path="", call=call)
        cache.set_dependant(call, dependant)

        assert cache.get_dependant(call) is dependant
        assert call in cache.dependants

    def test_distinct_callables_get_distinct_entries(self) -> None:
        """Two different callables never collide in the cache."""

        def first() -> None: ...

        def second() -> None: ...

        cache = DependantCache()
        cache.set_dependant(first, get_dependant(path="", call=first))
        cache.set_dependant(second, get_dependant(path="", call=second))

        assert cache.get_dependant(first) is not cache.get_dependant(second)
        assert len(cache.dependants) == 2

    def test_repeated_set_same_callable_stays_bounded(self) -> None:
        """Re-introspecting the same callable does not grow the cache."""

        def call() -> None: ...

        cache = DependantCache()
        for _ in range(100):
            cache.set_dependant(call, get_dependant(path="", call=call))

        assert len(cache.dependants) == 1


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

    async def test_generator_deps_cached_at_container_scope(self) -> None:
        """At CONTAINER scope (default), generator deps are shared across calls."""
        c = FastAPIContainer()
        dep1 = await c.get(IYieldDep)
        dep2 = await c.get(IYieldDep)
        assert dep1 is dep2


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


# --- Response / BackgroundTasks injection ----------------------------------


def _dep_with_response(response: Response) -> Response:
    """A dependency setting a header/cookie/status on the injected response."""
    response.status_code = 201
    response.headers["X-Custom"] = "value"
    response.set_cookie("session", "abc")
    return response


_background_ran: list[str] = []


def _dep_with_background_tasks(background_tasks: BackgroundTasks) -> BackgroundTasks:
    """A dependency registering a background task via ``add_task``."""

    async def _task(label: str) -> None:
        _background_ran.append(label)

    background_tasks.add_task(_task, "done")
    return background_tasks


class TestResponseInjection:
    async def test_response_param_resolves(self) -> None:
        """A dependency with a ``Response`` param resolves standalone."""
        c = FastAPIContainer()
        response = await c.get(_dep_with_response)
        assert isinstance(response, Response)

    async def test_response_mutations_do_not_raise(self) -> None:
        """Header/cookie/status mutations on the stub response are accepted."""
        c = FastAPIContainer()
        response = await c.get(_dep_with_response)
        assert response.status_code == 201
        assert response.headers["X-Custom"] == "value"
        assert "session=abc" in response.headers["set-cookie"]

    async def test_each_resolution_gets_fresh_response(self) -> None:
        """Non-cached resolutions get independent response stubs."""
        c = FastAPIContainer()
        r1 = await c.invoke(_dep_with_response)
        r2 = await c.invoke(_dep_with_response)
        assert r1 is not r2


class TestBackgroundTasksInjection:
    @pytest.fixture(autouse=True)
    def _reset_ran(self) -> Iterator[None]:
        _background_ran.clear()
        yield
        _background_ran.clear()

    async def test_background_tasks_param_resolves(self) -> None:
        """A dependency with a ``BackgroundTasks`` param resolves standalone."""
        c = FastAPIContainer()
        tasks = await c.get(_dep_with_background_tasks)
        assert isinstance(tasks, BackgroundTasks)

    async def test_add_task_is_accepted(self) -> None:
        """``add_task`` collects the task without raising."""
        c = FastAPIContainer()
        tasks = await c.get(_dep_with_background_tasks)
        assert len(tasks.tasks) == 1

    async def test_tasks_run_on_aclose(self) -> None:
        """CONTAINER-scoped collected tasks execute when the container closes."""
        c = FastAPIContainer()
        await c.get(_dep_with_background_tasks)
        assert _background_ran == []
        await c.aclose()
        assert _background_ran == ["done"]

    async def test_scoped_tasks_run_on_scope_close(self) -> None:
        """SCOPED collected tasks execute when their scope closes, not before."""
        c = FastAPIContainer(
            scopes={_dep_with_background_tasks: DependencyScope.SCOPED}
        )
        async with c.scope() as scope:
            await scope.get(_dep_with_background_tasks)
            assert _background_ran == []
        assert _background_ran == ["done"]


# --- concurrency-safe resolution -------------------------------------------

_slow_starts = 0
_slow_teardowns = 0


class SlowSingleton:
    pass


async def _slow_singleton_factory() -> AsyncIterator[SlowSingleton]:
    """Yield-based dependency that yields control mid-construction.

    The ``await`` between incrementing the start counter and yielding forces
    two gathered resolutions to interleave on a cache miss — the exact window
    the resolution lock must close.
    """
    global _slow_starts, _slow_teardowns
    _slow_starts += 1
    await asyncio.sleep(0)
    yield SlowSingleton()
    _slow_teardowns += 1


class TestConcurrentResolution:
    async def test_concurrent_get_of_same_dependency_yields_one_instance(
        self,
    ) -> None:
        """Racing get() of the same CONTAINER dependency shares one instance."""
        global _slow_starts, _slow_teardowns
        _slow_starts = 0
        _slow_teardowns = 0

        container = FastAPIContainer()
        a, b, c = await asyncio.gather(
            container.get(_slow_singleton_factory),  # type: ignore[arg-type]
            container.get(_slow_singleton_factory),  # type: ignore[arg-type]
            container.get(_slow_singleton_factory),  # type: ignore[arg-type]
        )

        assert a is b is c
        assert _slow_starts == 1

    async def test_concurrent_get_leaves_teardown_intact(self) -> None:
        """The shared instance registers a single teardown on the exit stack."""
        global _slow_starts, _slow_teardowns
        _slow_starts = 0
        _slow_teardowns = 0

        async with FastAPIContainer() as container:
            await asyncio.gather(
                container.get(_slow_singleton_factory),  # type: ignore[arg-type]
                container.get(_slow_singleton_factory),  # type: ignore[arg-type]
            )
            assert _slow_teardowns == 0
        assert _slow_teardowns == 1


# --- exposing resolved sub-dependencies (#21) ------------------------------


class TestResolvedSubDependencies:
    async def test_all_instances_includes_sub_dependencies(self) -> None:
        """all_instances() exposes the sub-deps resolved along the way."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep)
        keys = set(deps.all_instances())
        assert {RootDep, MiddleDep, LeafDep} <= keys

    async def test_all_instances_across_multiple_top_level(self) -> None:
        """A sub-dep shared by several top-level deps is reachable transitively."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep, IMiddleDep)
        assert deps.get(IRootDep).value() == "root(middle(leaf))"
        assert deps.get(IMiddleDep).value() == "middle(leaf)"
        assert {RootDep, MiddleDep, LeafDep} <= set(deps.all_instances())
        assert deps.get(ILeafDep, transitive=True) is deps.get(IMiddleDep).leaf

    async def test_intermediate_cache_hit_walks_subtree(self) -> None:
        """A cached middle node still contributes its sub-deps to a fresh parent."""
        c = FastAPIContainer()
        await c.resolve(IMiddleDep)  # cache Middle + Leaf
        deps = await c.resolve(IRootDep)  # Root fresh, Middle is a cache hit here
        assert {RootDep, MiddleDep, LeafDep} <= set(deps.all_instances())
        assert deps.get(ILeafDep, transitive=True) is await c.get(ILeafDep)

    async def test_empty_resolve_has_no_instances(self) -> None:
        """Resolving nothing yields an empty instance set."""
        deps = await FastAPIContainer().resolve()
        assert dict(deps.all_instances()) == {}

    async def test_keys_are_implementations_not_interfaces(self) -> None:
        """all_instances() is keyed by resolved impls, not the interface."""
        deps = await FastAPIContainer().resolve(IRootDep)
        keys = set(deps.all_instances())
        assert LeafDep in keys
        assert ILeafDep not in keys

    async def test_transitive_reaches_sub_dependencies(self) -> None:
        """get(transitive=True) returns sub-dep instances by interface or impl."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep)
        middle = deps.get(IMiddleDep, transitive=True)
        leaf = deps.get(ILeafDep, transitive=True)
        assert isinstance(middle, MiddleDep)
        assert isinstance(leaf, LeafDep)

    async def test_transitive_instances_are_the_ones_wired_in(self) -> None:
        """The exposed sub-deps are identical to those injected into the parent."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep)
        root = deps.get(IRootDep)
        assert deps.get(IMiddleDep, transitive=True) is root.middle
        assert deps.get(ILeafDep, transitive=True) is root.middle.leaf

    async def test_get_stays_top_level_by_default(self) -> None:
        """get()/optional() remain limited to the explicitly resolved deps."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep)
        with pytest.raises(KeyError, match="Pass transitive=True"):
            deps.get(IMiddleDep)
        assert deps.optional(ILeafDep) is None

    async def test_optional_transitive(self) -> None:
        """optional(transitive=True) returns the sub-dep, or None when absent."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep)
        assert deps.optional(ILeafDep, transitive=True) is not None
        assert deps.optional(plain_sync_dep, transitive=True) is None

    async def test_get_transitive_missing_raises(self) -> None:
        """get(transitive=True) raises KeyError for a callable never resolved."""
        c = FastAPIContainer()
        deps = await c.resolve(ILeafDep)
        with pytest.raises(KeyError, match="was not resolved"):
            deps.get(IRootDep, transitive=True)

    async def test_transitive_instance_matches_container_cache(self) -> None:
        """A CONTAINER sub-dep exposed here is the same the container caches."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep)
        cached_leaf = await c.get(ILeafDep)
        assert deps.get(ILeafDep, transitive=True) is cached_leaf

    async def test_transitive_survives_top_level_cache_hit(self) -> None:
        """Sub-deps stay reachable when the whole tree is served from cache."""
        c = FastAPIContainer()
        await c.resolve(IRootDep)  # warm the cache
        deps = await c.resolve(IRootDep)  # top-level cache hit, no re-instantiation
        assert {RootDep, MiddleDep, LeafDep} <= set(deps.all_instances())
        assert isinstance(deps.get(ILeafDep, transitive=True), LeafDep)

    async def test_cache_hit_preserves_resolution_order(self) -> None:
        """The cache-walk records sub-deps before their dependents, as a build does."""
        c = FastAPIContainer()
        await c.resolve(IRootDep)
        deps = await c.resolve(IRootDep)
        order = list(deps.all_instances())
        assert order.index(LeafDep) < order.index(MiddleDep) < order.index(RootDep)

    async def test_cache_hit_exposes_same_instances(self) -> None:
        """Sub-deps exposed after a cache hit are the cached instances themselves."""
        c = FastAPIContainer()
        first = await c.resolve(IRootDep)
        second = await c.resolve(IRootDep)
        assert second.get(IMiddleDep, transitive=True) is first.get(
            IMiddleDep, transitive=True
        )
        assert second.get(ILeafDep, transitive=True) is await c.get(ILeafDep)

    async def test_all_instances_is_read_only(self) -> None:
        """all_instances() returns an immutable view."""
        c = FastAPIContainer()
        deps = await c.resolve(ILeafDep)
        view = deps.all_instances()
        with pytest.raises(TypeError):
            view[LeafDep] = object()  # type: ignore[index]

    async def test_resolution_order_sub_deps_before_dependents(self) -> None:
        """Instances are ordered sub-dependencies first, then their dependents."""
        c = FastAPIContainer()
        deps = await c.resolve(IRootDep)
        order = list(deps.all_instances())
        assert order.index(LeafDep) < order.index(MiddleDep) < order.index(RootDep)

    async def test_cache_hit_skips_uncached_transients(self) -> None:
        """The cache-walk skips use_cache=False sub-deps absent from any cache."""
        c = FastAPIContainer()
        await c.resolve(root_uncached)  # build: fresh_dep transient is recorded
        deps = await c.resolve(root_uncached)  # cache hit: walk can't recover it
        keys = set(deps.all_instances())
        assert {root_uncached, consumer_a, consumer_b} <= keys
        assert fresh_dep not in keys

    async def test_use_cache_false_keeps_last_built_duplicate(self) -> None:
        """A use_cache=False sub-dep keeps only its last-built instance."""
        c = FastAPIContainer()
        deps = await c.resolve(root_uncached)
        a_value = deps.get(consumer_a, transitive=True)
        b_value = deps.get(consumer_b, transitive=True)
        assert a_value != b_value  # distinct builds — resolution semantics intact
        assert deps.get(fresh_dep, transitive=True) == b_value

    async def test_backward_compatible_single_arg_construction(self) -> None:
        """Constructing without the full map falls back to the top-level one."""
        deps = ResolvedDependencies({LeafDep: LeafDep()})
        assert deps.get(LeafDep, transitive=True) is deps.get(LeafDep)
        assert set(deps.all_instances()) == {LeafDep}


class TestInvokeResolved:
    async def test_invoke_resolved_get_returns_call_result(self) -> None:
        """The bag's get(call) yields the invocation result."""
        c = FastAPIContainer()
        deps = await c.invoke_resolved(handler_with_deps)
        assert deps.get(handler_with_deps) == "root(middle(leaf))"

    async def test_invoke_resolved_exposes_sub_dependencies(self) -> None:
        """Sub-deps resolved for the call are reachable on the returned bag."""
        c = FastAPIContainer()
        deps = await c.invoke_resolved(handler_with_deps)
        assert isinstance(deps.get(IRootDep, transitive=True), RootDep)
        assert isinstance(deps.get(ILeafDep, transitive=True), LeafDep)

    async def test_invoke_still_returns_result(self) -> None:
        """invoke() keeps returning the plain call result."""
        c = FastAPIContainer()
        assert await c.invoke(handler_with_deps) == "root(middle(leaf))"

    async def test_scope_invoke_resolved(self) -> None:
        """ResolutionScope.invoke_resolved mirrors the container method."""
        c = FastAPIContainer()
        async with c.scope() as scope:
            deps = await scope.invoke_resolved(handler_with_deps)
        assert deps.get(handler_with_deps) == "root(middle(leaf))"
        assert isinstance(deps.get(IMiddleDep, transitive=True), MiddleDep)


class TestDiamondResolution:
    async def test_shared_sub_dep_is_identical_across_branches(self) -> None:
        """A diamond's shared leaf is one instance, reachable transitively."""
        c = FastAPIContainer()
        deps = await c.resolve(IDiamondDep)
        shared = deps.get(IShared, transitive=True)
        diamond = deps.get(IDiamondDep)
        assert diamond.left.shared is shared
        assert diamond.right.shared is shared

    async def test_diamond_resolution_order(self) -> None:
        """The shared dep is recorded before both branches, branches before root."""
        deps = await FastAPIContainer().resolve(IDiamondDep)
        order = list(deps.all_instances())
        assert order.index(SharedDep) < order.index(LeftDep)
        assert order.index(SharedDep) < order.index(RightDep)
        assert order.index(LeftDep) < order.index(DiamondDep)
        assert order.index(RightDep) < order.index(DiamondDep)

    async def test_diamond_survives_cache_hit(self) -> None:
        """After a cache hit, the walk dedups the shared dep yet exposes the tree."""
        c = FastAPIContainer()
        await c.resolve(IDiamondDep)  # warm the cache
        deps = await c.resolve(IDiamondDep)  # cache hit → walk with shared-dep dedup
        assert {SharedDep, LeftDep, RightDep, DiamondDep} <= set(deps.all_instances())
        shared = deps.get(IShared, transitive=True)
        assert deps.get(ILeftDep, transitive=True).shared is shared
        assert deps.get(IRightDep, transitive=True).shared is shared


class TestInvokeResolvedLifetime:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Iterator[None]:
        _session_teardowns.clear()
        yield
        _session_teardowns.clear()

    async def test_scoped_sub_dep_torn_down_but_still_referenced(self) -> None:
        """A SCOPED sub-dep is torn down before invoke_resolved returns, yet the
        bag still references the (now closed) instance — the documented caveat."""
        c = FastAPIContainer(scopes={_session_factory: DependencyScope.SCOPED})
        deps = await c.invoke_resolved(_handler_with_session)
        assert deps.get(_handler_with_session) == "handled"
        session = deps.get(_session_factory, transitive=True)
        assert isinstance(session, Session)
        assert session in _session_teardowns


class ICyclic(RegistrableDependency):
    pass


class TestCyclicDependency:
    """A dependency that re-enters its own in-flight build is a cycle, surfaced
    as CyclicDependencyError rather than a lock deadlock. wait_for guards the
    suite: a regression fails fast instead of hanging forever."""

    @pytest.fixture(autouse=True)
    def _reset(self) -> Iterator[None]:
        AppState.reset_standalone()
        ICyclic.register(None)
        yield
        ICyclic.register(None)
        AppState.reset_standalone()

    async def test_lazy_singleton_resolving_its_own_interface_raises(self) -> None:
        # A genuine runtime cycle the fix must NOT mask: a lazy singleton whose
        # factory re-resolves, at run time, the very interface it is registered
        # for. Resolving the wrapper re-enters its own in-flight build.
        async def build(container: FastAPIContainer = Depends(get_container)) -> object:
            return await container.get(ICyclic)

        ICyclic.register(singleton(build, lazy=True))
        c = FastAPIContainer()
        with pytest.raises(CyclicDependencyError):
            await asyncio.wait_for(c.get(ICyclic.dependency()), timeout=5)

    async def test_lazy_singleton_on_implementation_class_resolves(self) -> None:
        # The #49 scenario, now supported: a lazy singleton whose factory is the
        # implementation class registered for its own interface. The lazy wrapper
        # builds the class directly instead of dereferencing back through the
        # interface, so there is no re-entry.
        class RedisCache(ICyclic):
            def __init__(self) -> None: ...

        ICyclic.register(singleton(RedisCache, lazy=True))
        c = FastAPIContainer()
        first = await asyncio.wait_for(c.get(ICyclic.dependency()), timeout=5)
        second = await c.get(ICyclic.dependency())
        assert first is second
        assert type(first).__name__ == "RedisCache"

    async def test_eager_singleton_on_implementation_class_is_fine(self) -> None:
        # The eager counterpart builds the class directly — no re-entry.
        class RedisCache(ICyclic):
            def __init__(self) -> None: ...

        ICyclic.register(singleton(RedisCache))
        c = FastAPIContainer()
        first = await asyncio.wait_for(c.get(ICyclic.dependency()), timeout=5)
        second = await c.get(ICyclic.dependency())
        assert first is second
        assert type(first).__name__ == "RedisCache"
