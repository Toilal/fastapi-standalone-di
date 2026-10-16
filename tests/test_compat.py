"""Tests for the version-independent callable-introspection shim."""

import functools
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, cast

import pytest

from fastapi_standalone_di import (
    FastAPIContainer,
    is_async_gen_callable,
    is_coroutine_callable,
    is_gen_callable,
)


def _sync_fn() -> str:
    return "x"


async def _coro_fn() -> str:
    return "x"


def _gen_fn() -> Iterator[str]:
    yield "x"


async def _async_gen_fn() -> AsyncIterator[str]:
    yield "x"


class _CallableClass:
    def __call__(self) -> str:
        return "x"


class _AsyncCallableClass:
    async def __call__(self) -> str:
        return "x"


class _GenCallableClass:
    def __call__(self) -> Iterator[str]:
        yield "x"


class _AsyncGenCallableClass:
    async def __call__(self) -> AsyncIterator[str]:
        yield "x"


class TestIsCoroutineCallable:
    def test_true_for_coroutine(self) -> None:
        assert is_coroutine_callable(_coro_fn) is True

    def test_false_for_sync(self) -> None:
        assert is_coroutine_callable(_sync_fn) is False

    def test_false_for_class(self) -> None:
        assert is_coroutine_callable(_CallableClass) is False

    def test_true_for_async_dunder_call(self) -> None:
        assert is_coroutine_callable(_AsyncCallableClass()) is True

    def test_false_for_sync_dunder_call(self) -> None:
        assert is_coroutine_callable(_CallableClass()) is False

    def test_unwraps_partial(self) -> None:
        assert is_coroutine_callable(functools.partial(_coro_fn)) is True


class TestIsGenCallable:
    def test_true_for_generator(self) -> None:
        assert is_gen_callable(_gen_fn) is True

    def test_false_for_plain(self) -> None:
        assert is_gen_callable(_sync_fn) is False

    def test_true_for_gen_dunder_call(self) -> None:
        assert is_gen_callable(_GenCallableClass()) is True

    def test_unwraps_partial(self) -> None:
        assert is_gen_callable(functools.partial(_gen_fn)) is True


class TestIsAsyncGenCallable:
    def test_true_for_async_generator(self) -> None:
        assert is_async_gen_callable(_async_gen_fn) is True

    def test_false_for_coroutine(self) -> None:
        assert is_async_gen_callable(_coro_fn) is False

    def test_true_for_async_gen_dunder_call(self) -> None:
        assert is_async_gen_callable(_AsyncGenCallableClass()) is True

    def test_unwraps_partial(self) -> None:
        assert is_async_gen_callable(functools.partial(_async_gen_fn)) is True


@functools.wraps(_coro_fn)
def _wrapped_coro_fn(*args: Any, **kwargs: Any) -> Any:
    return _coro_fn(*args, **kwargs)


@functools.wraps(_gen_fn)
def _wrapped_gen_fn(*args: Any, **kwargs: Any) -> Any:
    return _gen_fn(*args, **kwargs)


# Expected ``(coroutine, generator, async generator)`` classification, as FastAPI
# 0.143.1 computes it in ``fastapi.dependencies.models``.
_Kinds = tuple[bool, bool, bool]
_FASTAPI_CLASSIFICATION: list[tuple[str, Callable[..., Any], _Kinds]] = [
    ("function", _sync_fn, (False, False, False)),
    ("coroutine", _coro_fn, (True, False, False)),
    ("generator", _gen_fn, (False, True, False)),
    ("async generator", _async_gen_fn, (False, False, True)),
    ("class", _CallableClass, (False, False, False)),
    ("class with a generator __call__", _GenCallableClass, (False, False, False)),
    ("instance with an async __call__", _AsyncCallableClass(), (True, False, False)),
    ("partial of a coroutine", functools.partial(_coro_fn), (True, False, False)),
    ("wraps wrapper of a coroutine", _wrapped_coro_fn, (True, False, False)),
    ("wraps wrapper of a generator", _wrapped_gen_fn, (False, True, False)),
    (
        "partial of a wraps wrapper of a coroutine",
        functools.partial(_wrapped_coro_fn),
        (True, False, False),
    ),
]


@pytest.mark.parametrize(
    ("call", "expected"),
    [(call, expected) for _, call, expected in _FASTAPI_CLASSIFICATION],
    ids=[name for name, _, _ in _FASTAPI_CLASSIFICATION],
)
def test_classification_matches_fastapi(
    call: Callable[..., Any], expected: _Kinds
) -> None:
    assert (
        is_coroutine_callable(call),
        is_gen_callable(call),
        is_async_gen_callable(call),
    ) == expected


def test_non_callable_is_none_of_the_kinds() -> None:
    not_callable = cast("Callable[..., Any]", object())

    assert not is_coroutine_callable(not_callable)
    assert not is_gen_callable(not_callable)
    assert not is_async_gen_callable(not_callable)


async def test_container_awaits_a_wraps_wrapper_of_a_coroutine() -> None:
    assert await FastAPIContainer().get(_wrapped_coro_fn) == "x"


async def test_container_enters_a_wraps_wrapper_of_a_generator() -> None:
    container = FastAPIContainer()

    assert await container.get(_wrapped_gen_fn) == "x"
    await container.aclose()
