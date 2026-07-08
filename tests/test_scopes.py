"""Tests for the dependency-scope system (CONTAINER vs SCOPED)."""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import pytest
from fastapi import Depends

from fastapi_standalone_di import (
    DependencyScope,
    FastAPIContainer,
    RegistrableDependency,
    ScopeError,
)
from fastapi_standalone_di._compat import DEPENDS_SUPPORTS_SCOPE

CONTAINER = DependencyScope.CONTAINER
SCOPED = DependencyScope.SCOPED


class Conn:
    def __init__(self, ident: int) -> None:
        self.id = ident


class _Counter:
    n_open = 0
    n_close = 0
    live = 0


@pytest.fixture(autouse=True)
def _reset() -> None:
    _Counter.n_open = 0
    _Counter.n_close = 0
    _Counter.live = 0


async def get_conn() -> AsyncIterator[Conn]:
    _Counter.n_open += 1
    _Counter.live += 1
    conn = Conn(_Counter.n_open)
    try:
        yield conn
    finally:
        _Counter.n_close += 1
        _Counter.live -= 1


class ServiceA:
    def __init__(self, conn: Conn = Depends(get_conn)) -> None:
        self.conn = conn


class ServiceB:
    def __init__(self, conn: Conn = Depends(get_conn)) -> None:
        self.conn = conn


class ServiceSharedConns:
    def __init__(
        self,
        c1: Conn = Depends(get_conn),
        c2: Conn = Depends(get_conn),
    ) -> None:
        self.c1 = c1
        self.c2 = c2


class ServiceFreshConns:
    def __init__(
        self,
        c1: Conn = Depends(get_conn, use_cache=False),
        c2: Conn = Depends(get_conn, use_cache=False),
    ) -> None:
        self.c1 = c1
        self.c2 = c2


class TestContainerScope:
    async def test_yield_dep_shared_across_consumers(self) -> None:
        """CONTAINER + use_cache=True: one instance shared, opened once."""
        c = FastAPIContainer()
        deps = await c.resolve(ServiceA, ServiceB)
        assert deps.get(ServiceA).conn is deps.get(ServiceB).conn
        assert _Counter.n_open == 1

    async def test_yield_dep_torn_down_once_at_aclose(self) -> None:
        c = FastAPIContainer()
        await c.resolve(ServiceA, ServiceB)
        assert _Counter.n_close == 0
        await c.aclose()
        assert _Counter.n_close == 1

    async def test_shared_within_single_service(self) -> None:
        c = FastAPIContainer()
        svc = await c.get(ServiceSharedConns)
        assert svc.c1 is svc.c2
        assert _Counter.n_open == 1


class TestUseCache:
    async def test_use_cache_false_yields_fresh_per_consumer(self) -> None:
        """use_cache=False: a fresh instance at each injection point."""
        c = FastAPIContainer()
        svc = await c.get(ServiceFreshConns)
        assert svc.c1 is not svc.c2
        assert _Counter.n_open == 2

    async def test_use_cache_false_teardowns_deferred_to_aclose(self) -> None:
        c = FastAPIContainer()
        await c.get(ServiceFreshConns)
        assert _Counter.n_close == 0
        await c.aclose()
        assert _Counter.n_close == 2


class TestScopedLifetime:
    async def test_fresh_per_scope_and_torn_down_at_scope_exit(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)

        async with c.scope() as s1:
            a1 = await s1.get(ServiceA)
            assert _Counter.live == 1
        assert _Counter.live == 0
        assert _Counter.n_close == 1

        async with c.scope() as s2:
            a2 = await s2.get(ServiceA)
            assert _Counter.live == 1
        assert _Counter.live == 0

        assert a1 is not a2
        assert _Counter.n_open == 2
        assert _Counter.n_close == 2

    async def test_shared_within_a_scope(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        async with c.scope() as s:
            a = await s.get(ServiceA)
            b = await s.get(ServiceB)
            assert a.conn is b.conn
            assert _Counter.n_open == 1

    async def test_container_scoped_survive_scope_exit(self) -> None:
        """CONTAINER deps resolved inside a scope outlive it."""
        c = FastAPIContainer()  # default CONTAINER
        async with c.scope() as s:
            a = await s.get(ServiceA)
        assert _Counter.n_close == 0  # still owned by the container
        # same container-scoped instance is reused afterwards
        assert (await c.get(ServiceA)) is a
        await c.aclose()
        assert _Counter.n_close == 1


class TestEscapeWithoutScope:
    async def test_scoped_get_without_scope_raises(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        with pytest.raises(ScopeError, match="SCOPED"):
            await c.get(ServiceA)

    async def test_scoped_generator_get_without_scope_raises(self) -> None:
        c = FastAPIContainer(scopes={get_conn: SCOPED})
        with pytest.raises(ScopeError, match="SCOPED"):
            await c.get(get_conn)  # type: ignore[arg-type]
        assert _Counter.n_open == 0  # never entered


class TestInvoke:
    async def test_invoke_opens_implicit_scope(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)

        def handler(a: ServiceA = Depends(ServiceA)) -> int:
            assert _Counter.live == 1
            return a.conn.id

        result = await c.invoke(handler)
        assert result == 1
        assert _Counter.live == 0  # torn down after invoke returns
        assert _Counter.n_close == 1


class TestScopeConfiguration:
    async def test_default_scope_dict_none_key(self) -> None:
        container_default = FastAPIContainer(default_scope={None: CONTAINER})
        assert isinstance(await container_default.get(ServiceA), ServiceA)
        with pytest.raises(ScopeError):
            await FastAPIContainer(default_scope={None: SCOPED}).get(ServiceA)

    async def test_default_scope_dict_without_matching_key_falls_back(self) -> None:
        # The dict has no None key; a top-level dep (FastAPI scope None) must
        # fall back to the hard-coded CONTAINER default rather than KeyError.
        c = FastAPIContainer(default_scope={"function": SCOPED})
        assert isinstance(await c.get(ServiceA), ServiceA)

    @pytest.mark.skipif(
        not DEPENDS_SUPPORTS_SCOPE, reason="FastAPI Depends() has no scope= param"
    )
    async def test_default_scope_dict_subdep_scope_absent_falls_back(self) -> None:
        # get_conn carries scope="request", absent from the dict -> fall back to
        # the None entry (CONTAINER) for the sub-dependency too.
        def parent(conn: Conn = Depends(get_conn, scope="request")) -> Conn:  # type: ignore[call-arg]
            return conn

        c = FastAPIContainer(default_scope={None: CONTAINER})
        assert isinstance(await c.get(parent), Conn)  # type: ignore[arg-type]

    @pytest.mark.skipif(
        not DEPENDS_SUPPORTS_SCOPE, reason="FastAPI Depends() has no scope= param"
    )
    async def test_default_scope_dict_maps_fastapi_depends_scope(self) -> None:
        def parent(conn: Conn = Depends(get_conn, scope="request")) -> Conn:  # type: ignore[call-arg]
            return conn

        # None (parent) -> CONTAINER, "request" (get_conn) -> SCOPED:
        # the container would capture the scoped conn -> captive dependency.
        c = FastAPIContainer(default_scope={None: CONTAINER, "request": SCOPED})
        with pytest.raises(ScopeError, match="captive"):
            async with c.scope() as s:
                await s.get(parent)

    async def test_scopes_override_keyed_by_interface(self) -> None:
        class IThing(ABC, RegistrableDependency):
            @abstractmethod
            def v(self) -> str: ...

        class Thing(IThing):
            def v(self) -> str:
                return "thing"

        IThing.register(Thing)
        try:
            c = FastAPIContainer(scopes={IThing: SCOPED})
            with pytest.raises(ScopeError, match="SCOPED"):
                await c.get(IThing)
            async with c.scope() as s:
                assert (await s.get(IThing)).v() == "thing"
        finally:
            IThing.register(None)


class _Boom(RuntimeError):
    pass


class ServiceRaisingAfterConn:
    """Depends on an already-entered yield generator, then blows up."""

    def __init__(self, conn: Conn = Depends(get_conn)) -> None:
        raise _Boom("init failed after conn was opened")


class TestTeardownOnError:
    async def test_container_teardown_runs_when_dependent_raises(self) -> None:
        """A yield dep entered before a sibling/consumer raises is still closed."""
        c = FastAPIContainer()
        with pytest.raises(_Boom):
            await c.get(ServiceRaisingAfterConn)
        assert _Counter.n_open == 1
        # The generator is entered on the container stack; teardown defers to aclose.
        assert _Counter.n_close == 0
        await c.aclose()
        assert _Counter.n_close == 1
        assert _Counter.live == 0

    async def test_scope_teardown_runs_when_dependent_raises(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        with pytest.raises(_Boom):
            async with c.scope() as s:
                await s.get(ServiceRaisingAfterConn)
        assert _Counter.n_open == 1
        assert _Counter.n_close == 1
        assert _Counter.live == 0

    async def test_invoke_teardown_runs_when_handler_raises(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)

        def handler(conn: Conn = Depends(get_conn)) -> None:
            raise _Boom("handler failed after conn was opened")

        with pytest.raises(_Boom):
            await c.invoke(handler)
        assert _Counter.n_open == 1
        assert _Counter.n_close == 1
        assert _Counter.live == 0


class TestScopeOptional:
    async def test_optional_returns_resolved_instance(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        async with c.scope() as s:
            resolved = await s.optional(ServiceA)
            assert isinstance(resolved, ServiceA)

    async def test_optional_returns_none_for_unrelated_dependency(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        async with c.scope() as s:
            deps = await s.resolve(ServiceA)
            assert deps.optional(ServiceB) is None


class TestScopedUseCache:
    async def test_use_cache_false_yields_fresh_within_a_scope(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        async with c.scope() as s:
            svc = await s.get(ServiceFreshConns)
            assert svc.c1 is not svc.c2
            assert _Counter.n_open == 2
            assert _Counter.live == 2
        assert _Counter.n_close == 2
        assert _Counter.live == 0

    async def test_concurrent_scoped_resolution_shares_one_instance(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        async with c.scope() as s:
            a, b = await asyncio.gather(s.get(ServiceA), s.get(ServiceB))
            # Both resolutions race on the same scoped get_conn: the lock must
            # serialise them so a single connection is opened and shared.
            assert a.conn is b.conn
            assert _Counter.n_open == 1


class TestCaptiveDependency:
    async def test_container_dep_on_scoped_dep_raises(self) -> None:
        # ServiceA is CONTAINER (default) but get_conn is forced SCOPED.
        c = FastAPIContainer(scopes={get_conn: SCOPED})
        with pytest.raises(ScopeError, match="captive"):
            async with c.scope() as s:
                await s.get(ServiceA)

    async def test_scoped_dep_on_scoped_dep_is_fine(self) -> None:
        c = FastAPIContainer(default_scope=SCOPED)
        async with c.scope() as s:
            a = await s.get(ServiceA)
            assert isinstance(a.conn, Conn)
