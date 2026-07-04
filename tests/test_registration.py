"""Tests for fastapi_standalone_di.registration."""

from abc import ABC, abstractmethod
from collections.abc import Iterator

import fastapi.params
import pytest

from fastapi_standalone_di import RegistrableDependency
from fastapi_standalone_di.registration import (
    _DEPENDS_SUPPORTS_SCOPE,
    _Depends,
    patch_for_registrable_dependency_support,
)


class IService(ABC, RegistrableDependency):
    @abstractmethod
    def name(self) -> str: ...


class ServiceImpl(IService):
    def name(self) -> str:
        return "impl"


@pytest.fixture(autouse=True)
def _clear_registration() -> Iterator[None]:
    yield
    IService.register(None)


class TestRegistration:
    def test_register_and_dependency(self) -> None:
        IService.register(ServiceImpl)
        assert IService.dependency() is ServiceImpl

    def test_impl_classproperty(self) -> None:
        IService.register(ServiceImpl)
        assert IService.impl is ServiceImpl

    def test_dependency_without_registration_raises(self) -> None:
        with pytest.raises(RuntimeError, match="No implementation registered"):
            IService.dependency()

    def test_impl_without_registration_raises(self) -> None:
        with pytest.raises(RuntimeError, match="No implementation registered"):
            _ = IService.impl

    def test_register_none_clears(self) -> None:
        IService.register(ServiceImpl)
        IService.register(None)
        with pytest.raises(RuntimeError):
            IService.dependency()


class TestDependsPatch:
    def test_patch_swaps_depends_class(self) -> None:
        original = fastapi.params.Depends
        try:
            assert patch_for_registrable_dependency_support() is True
            assert fastapi.params.Depends is _Depends
        finally:
            fastapi.params.Depends = original

    def test_patch_is_idempotent(self) -> None:
        original = fastapi.params.Depends
        try:
            patch_for_registrable_dependency_support()
            # Already patched: a second call is a no-op returning False.
            assert patch_for_registrable_dependency_support() is False
        finally:
            fastapi.params.Depends = original

    def test_depends_dereferences_registrable(self) -> None:
        IService.register(ServiceImpl)
        dep = _Depends(IService)
        assert dep.dependency is ServiceImpl

    def test_depends_keeps_plain_callable(self) -> None:
        def plain() -> str:
            return "x"

        dep = _Depends(plain)
        assert dep.dependency is plain

    def test_patched_depends_dereferences_at_construction(self) -> None:
        IService.register(ServiceImpl)
        original = fastapi.params.Depends
        try:
            patch_for_registrable_dependency_support()
            dep = fastapi.params.Depends(IService)
            assert isinstance(dep, _Depends)
            assert dep.dependency is ServiceImpl
        finally:
            fastapi.params.Depends = original


class TestDependsBehaviour:
    def test_dependency_none_when_unset(self) -> None:
        assert _Depends().dependency is None

    def test_plain_class_is_returned_as_is(self) -> None:
        class Plain:
            pass

        assert _Depends(Plain).dependency is Plain

    def test_scope_and_use_cache_are_forwarded(self) -> None:
        dep = _Depends(scope="request", use_cache=False)  # type: ignore[call-arg]
        assert dep.use_cache is False
        if _DEPENDS_SUPPORTS_SCOPE:
            assert dep.scope == "request"  # type: ignore[attr-defined]
