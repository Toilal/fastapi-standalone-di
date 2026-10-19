"""Tests for the loop-safe execution model of sync dependencies."""

import threading
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

import pytest
from _asgi import asgi_get
from fastapi import Depends, FastAPI
from starlette.concurrency import run_in_threadpool

from fastapi_standalone_di import (
    AppState,
    DependencyScope,
    FastAPIContainer,
    get_app_state,
    get_container,
    is_loop_safe,
    loop_safe,
    singleton,
)
from fastapi_standalone_di.execution import LOOP_SAFE_ATTR
from fastapi_standalone_di.resolve import _get_container_optional


class Plain:
    def __init__(self) -> None:
        self.thread = threading.get_ident()


@loop_safe
class Marked:
    def __init__(self) -> None:
        self.thread = threading.get_ident()


class UnmarkedChild(Marked):
    def __init__(self) -> None:
        self.thread = threading.get_ident()


def plain_fn() -> int:
    return threading.get_ident()


@loop_safe
def marked_fn() -> int:
    return threading.get_ident()


def sync_gen() -> Iterator[int]:
    yield threading.get_ident()


class Boom:
    def __init__(self) -> None:
        raise ValueError("boom")


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


def test_marker_attribute_is_the_contract_shared_with_fastapi_cache_di() -> None:
    assert LOOP_SAFE_ATTR == "__fastapi_loop_safe__"


def test_loop_safe_returns_the_same_object() -> None:
    def fn() -> None: ...

    assert loop_safe(fn) is fn
    assert is_loop_safe(fn)


def test_marker_set_by_hand_is_recognised() -> None:
    class Foreign:
        __fastapi_loop_safe__ = True

    assert is_loop_safe(Foreign)


def test_unmarked_callables_are_not_loop_safe() -> None:
    assert not is_loop_safe(Plain)
    assert not is_loop_safe(plain_fn)


def test_marker_is_not_inherited() -> None:
    assert is_loop_safe(Marked)
    assert not is_loop_safe(UnmarkedChild)


def test_partial_of_a_marked_function_is_loop_safe() -> None:
    assert is_loop_safe(partial(marked_fn))


@pytest.mark.parametrize(
    "dependency", [get_app_state, get_container, _get_container_optional]
)
def test_built_in_dependencies_are_loop_safe(dependency: Callable[..., Any]) -> None:
    assert is_loop_safe(dependency)


# ---------------------------------------------------------------------------
# Container dispatch
# ---------------------------------------------------------------------------


async def test_marked_callables_run_on_the_loop_by_default() -> None:
    loop_thread = threading.get_ident()
    container = FastAPIContainer()

    assert (await container.get(Marked)).thread == loop_thread
    assert await container.get(marked_fn) == loop_thread


async def test_unmarked_callables_run_in_the_threadpool_by_default() -> None:
    loop_thread = threading.get_ident()
    container = FastAPIContainer()

    assert (await container.get(Plain)).thread != loop_thread
    assert (await container.get(UnmarkedChild)).thread != loop_thread
    assert await container.get(plain_fn) != loop_thread


async def test_inline_classes_runs_every_class_on_the_loop() -> None:
    loop_thread = threading.get_ident()
    container = FastAPIContainer(inline_classes=True)

    assert (await container.get(Plain)).thread == loop_thread
    assert (await container.get(UnmarkedChild)).thread == loop_thread


async def test_inline_classes_runs_a_partial_of_a_class_on_the_loop() -> None:
    loop_thread = threading.get_ident()
    container = FastAPIContainer(inline_classes=True)

    assert (await container.get(partial(Plain))).thread == loop_thread


async def test_inline_classes_keeps_functions_and_generators_in_the_threadpool() -> (
    None
):
    loop_thread = threading.get_ident()
    container = FastAPIContainer(inline_classes=True)

    assert await container.get(plain_fn) != loop_thread
    assert await container.get(sync_gen) != loop_thread
    await container.aclose()


async def test_resolution_scope_follows_its_container() -> None:
    loop_thread = threading.get_ident()
    container = FastAPIContainer(
        inline_classes=True, scopes={Plain: DependencyScope.SCOPED}
    )

    async with container.scope() as scope:
        assert (await scope.get(Plain)).thread == loop_thread


async def test_unmarked_override_of_a_marked_original_uses_the_threadpool() -> None:
    loop_thread = threading.get_ident()
    container = FastAPIContainer(dependency_overrides={marked_fn: plain_fn})

    assert await container.get(marked_fn) != loop_thread


async def test_marked_override_of_an_unmarked_original_runs_on_the_loop() -> None:
    loop_thread = threading.get_ident()
    container = FastAPIContainer(dependency_overrides={plain_fn: marked_fn})

    assert await container.get(plain_fn) == loop_thread


async def test_unmarked_override_of_get_app_state_uses_the_threadpool() -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def override() -> AppState:
        threads.append(threading.get_ident())
        return AppState.standalone()

    container = FastAPIContainer(dependency_overrides={get_app_state: override})
    await container.get(get_app_state)

    assert len(threads) == 1
    assert threads[0] != loop_thread


@pytest.mark.parametrize("inline_classes", [False, True])
async def test_seeded_built_in_dependencies_never_reach_the_threadpool(
    inline_classes: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    hops: list[Callable[..., Any]] = []

    async def spy(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        hops.append(func)
        return await run_in_threadpool(func, *args, **kwargs)

    monkeypatch.setattr("fastapi_standalone_di.resolve.run_in_threadpool", spy)
    container = FastAPIContainer(inline_classes=inline_classes)

    for _ in range(2):
        assert await container.get(get_app_state) is AppState.standalone()
        assert await container.get(get_container) is container
        container.clear_cache()

    assert hops == []


async def test_exception_from_an_inlined_init_propagates() -> None:
    container = FastAPIContainer(inline_classes=True)

    with pytest.raises(ValueError, match="boom"):
        await container.get(Boom)


@pytest.mark.parametrize("inline_classes", [False, True])
async def test_lifetimes_and_use_cache_are_unchanged(inline_classes: bool) -> None:
    created: list[object] = []

    class Counted:
        def __init__(self) -> None:
            created.append(self)

    class Consumer:
        def __init__(
            self,
            shared_a: Counted = Depends(Counted),
            shared_b: Counted = Depends(Counted),
            fresh: Counted = Depends(Counted, use_cache=False),
        ) -> None:
            self.shared_a = shared_a
            self.shared_b = shared_b
            self.fresh = fresh

    container = FastAPIContainer(
        inline_classes=inline_classes,
        scopes={Consumer: DependencyScope.SCOPED, Counted: DependencyScope.SCOPED},
    )

    async with container.scope() as scope:
        first = await scope.get(Consumer)
    async with container.scope() as scope:
        second = await scope.get(Consumer)

    assert first.shared_a is first.shared_b
    assert first.fresh is not first.shared_a
    assert second.shared_a is not first.shared_a
    assert len(created) == 4


# ---------------------------------------------------------------------------
# singleton()
# ---------------------------------------------------------------------------


async def test_singleton_of_a_marked_factory_runs_on_the_loop() -> None:
    loop_thread = threading.get_ident()
    get_marked = singleton(Marked, key="execution-test-marked")

    assert (await FastAPIContainer().get(get_marked)).thread == loop_thread


async def test_singleton_ignores_the_container_inline_classes_option() -> None:
    loop_thread = threading.get_ident()
    get_plain = singleton(Plain, key="execution-test-plain")
    container = FastAPIContainer(inline_classes=True)

    assert (await container.get(get_plain)).thread != loop_thread


# ---------------------------------------------------------------------------
# FastAPI requests under fastapi-cache-di's threadpool patch
# ---------------------------------------------------------------------------


@pytest.fixture
def threadpool_hops(monkeypatch: pytest.MonkeyPatch) -> list[Callable[..., Any]]:
    """Reproduce fastapi-cache-di's patch and record what still hops.

    Like ``patch_fastapi_run_in_threadpool``, the replacement calls loop-safe
    dependencies inline and sends every other one to the threadpool.
    """
    hops: list[Callable[..., Any]] = []

    async def run_loop_safe_inline(
        func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        if is_loop_safe(func):
            return func(*args, **kwargs)
        hops.append(func)
        return await run_in_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(
        "fastapi.dependencies.utils.run_in_threadpool", run_loop_safe_inline
    )
    return hops


async def test_built_in_dependencies_run_inline_under_the_patch(
    threadpool_hops: list[Callable[..., Any]],
) -> None:
    get_lazy = singleton(Plain, key="execution-test-lazy", lazy=True)
    app = FastAPI()
    container = FastAPIContainer(app_state=AppState.from_app(app))
    app.state.container = container

    @app.get("/deps")
    async def route(
        app_state: AppState = Depends(get_app_state),
        resolved: FastAPIContainer = Depends(get_container),
        lazy: Plain = Depends(get_lazy),
        _: int = Depends(plain_fn),
    ) -> dict[str, bool]:
        return {
            "app_state": app_state.get("container") is container,
            "container": resolved is container,
            "lazy": isinstance(lazy, Plain),
        }

    body = await asgi_get(app, "/deps")

    assert body == {"app_state": True, "container": True, "lazy": True}
    assert threadpool_hops == [plain_fn]


async def test_unmarked_override_of_get_app_state_hops_under_the_patch(
    threadpool_hops: list[Callable[..., Any]],
) -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def override() -> AppState:
        threads.append(threading.get_ident())
        return AppState.standalone()

    app = FastAPI()
    app.dependency_overrides[get_app_state] = override

    @app.get("/state")
    async def route(
        app_state: AppState = Depends(get_app_state),
    ) -> dict[str, bool]:
        return {"overridden": app_state is AppState.standalone()}

    body = await asgi_get(app, "/state")

    assert body == {"overridden": True}
    assert threadpool_hops == [override]
    assert len(threads) == 1
    assert threads[0] != loop_thread
