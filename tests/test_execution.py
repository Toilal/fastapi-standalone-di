"""Tests for the loop-safe execution model of sync dependencies."""

import threading
from collections.abc import Iterator
from functools import partial

import pytest
from fastapi import Depends

from fastapi_standalone_di import (
    DependencyScope,
    FastAPIContainer,
    is_loop_safe,
    loop_safe,
    singleton,
)
from fastapi_standalone_di.execution import LOOP_SAFE_ATTR


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
