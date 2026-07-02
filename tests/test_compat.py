"""Tests for the version-independent callable-introspection shim."""

import functools
from collections.abc import AsyncIterator, Iterator

from fastapi_standalone_di._compat import (
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


class TestIsCoroutineCallable:
    def test_true_for_coroutine(self) -> None:
        assert is_coroutine_callable(_coro_fn) is True

    def test_false_for_sync(self) -> None:
        assert is_coroutine_callable(_sync_fn) is False

    def test_false_for_class(self) -> None:
        assert is_coroutine_callable(_CallableClass) is False

    def test_true_for_async_dunder_call(self) -> None:
        assert is_coroutine_callable(_AsyncCallableClass()) is True

    def test_unwraps_partial(self) -> None:
        assert is_coroutine_callable(functools.partial(_coro_fn)) is True


class TestIsGenCallable:
    def test_true_for_generator(self) -> None:
        assert is_gen_callable(_gen_fn) is True

    def test_false_for_plain(self) -> None:
        assert is_gen_callable(_sync_fn) is False


class TestIsAsyncGenCallable:
    def test_true_for_async_generator(self) -> None:
        assert is_async_gen_callable(_async_gen_fn) is True

    def test_false_for_coroutine(self) -> None:
        assert is_async_gen_callable(_coro_fn) is False
