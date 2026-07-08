"""Tests for fastapi_standalone_di.registration."""

from abc import ABC, abstractmethod
from collections.abc import Iterator

import fastapi.params
import pytest

from fastapi_standalone_di import RegistrableDependency
from fastapi_standalone_di.registration import (
    _PATCHED_FLAG,
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


@pytest.fixture
def _unpatch_depends() -> Iterator[None]:
    """Undo the in-place patch so each patch test starts from a clean class."""
    depends = fastapi.params.Depends
    had_property = "dependency" in depends.__dict__
    try:
        yield
    finally:
        if not had_property and "dependency" in depends.__dict__:
            del depends.dependency
        if hasattr(depends, _PATCHED_FLAG):
            delattr(depends, _PATCHED_FLAG)


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


@pytest.mark.usefixtures("_unpatch_depends")
class TestDependsPatch:
    def test_patch_keeps_the_class_identity(self) -> None:
        original = fastapi.params.Depends
        assert patch_for_registrable_dependency_support() is True
        # The class object is mutated in place, not swapped: pre-existing
        # instances keep passing ``isinstance(_, fastapi.params.Depends)``.
        assert fastapi.params.Depends is original

    def test_patch_is_idempotent(self) -> None:
        assert patch_for_registrable_dependency_support() is True
        assert patch_for_registrable_dependency_support() is False

    def test_depends_dereferences_registrable(self) -> None:
        IService.register(ServiceImpl)
        patch_for_registrable_dependency_support()
        assert fastapi.params.Depends(IService).dependency is ServiceImpl

    def test_depends_keeps_plain_callable(self) -> None:
        def plain() -> str:
            return "x"

        patch_for_registrable_dependency_support()
        assert fastapi.params.Depends(plain).dependency is plain

    def test_depends_built_before_patch_is_dereferenced(self) -> None:
        """The crux of the fix: a ``Depends`` created before the patch runs."""
        IService.register(ServiceImpl)
        dep = fastapi.params.Depends(IService)
        assert isinstance(dep, fastapi.params.Depends)
        patch_for_registrable_dependency_support()
        # Still the same instance, still an instance of the (unchanged) class,
        # now dereferenced to the registered implementation.
        assert isinstance(dep, fastapi.params.Depends)
        assert dep.dependency is ServiceImpl


@pytest.mark.usefixtures("_unpatch_depends")
class TestDependsBehaviour:
    def test_dependency_none_when_unset(self) -> None:
        patch_for_registrable_dependency_support()
        assert fastapi.params.Depends().dependency is None

    def test_plain_class_is_returned_as_is(self) -> None:
        class Plain:
            pass

        patch_for_registrable_dependency_support()
        assert fastapi.params.Depends(Plain).dependency is Plain

    def test_use_cache_is_preserved(self) -> None:
        patch_for_registrable_dependency_support()
        assert fastapi.params.Depends(use_cache=False).use_cache is False


@pytest.mark.usefixtures("_unpatch_depends")
class TestSingletonRouteConstruction:
    """Regression for #40: ``@singleton`` provider behind a registrable port,
    reached from a route, must not break FastAPI's route/OpenAPI analysis
    regardless of when the wrapper's ``Depends`` objects were built."""

    def test_eager_singleton_built_before_patch(self) -> None:
        from fastapi import Depends, FastAPI

        from fastapi_standalone_di import singleton

        @singleton(key="reg40_eager")
        def build() -> object:
            return object()

        patch_for_registrable_dependency_support()

        class Port(ABC, RegistrableDependency): ...

        Port.register(build)

        app = FastAPI()

        @app.get("/eager")
        def route(dep: Port = Depends(Port)) -> dict[str, str]:
            return {}

        assert any(r.path == "/eager" for r in app.routes)  # type: ignore[attr-defined]

    def test_lazy_singleton_with_patch_applied_first(self) -> None:
        from fastapi import Depends, FastAPI

        from fastapi_standalone_di import singleton

        patch_for_registrable_dependency_support()

        @singleton(key="reg40_lazy", lazy=True)
        async def build() -> object:
            yield object()

        class Port(ABC, RegistrableDependency): ...

        Port.register(build)

        app = FastAPI()

        @app.get("/lazy")
        def route(dep: Port = Depends(Port)) -> dict[str, str]:
            return {}

        assert any(r.path == "/lazy" for r in app.routes)  # type: ignore[attr-defined]
