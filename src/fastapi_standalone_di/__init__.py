"""Use FastAPI's dependency injection outside of any web/ASGI context."""

from fastapi_standalone_di.app_state import (
    AppState,
    get_app_state,
    set_app_state_value,
)
from fastapi_standalone_di.discovery import (
    AutoBindingError,
    Binding,
    ConflictSolver,
    auto_bindings,
    register_bindings,
)
from fastapi_standalone_di.registration import (
    RegistrableDependency,
    patch_for_registrable_dependency_support,
)
from fastapi_standalone_di.resolve import (
    CyclicDependencyError,
    DependantCache,
    DependencyOverrides,
    DependencyScope,
    FastAPIContainer,
    MissingParameterError,
    ParameterError,
    ParameterValidationError,
    ParamSource,
    ResolutionScope,
    ResolvedDependencies,
    ScopeError,
    container_lifespan,
    get_container,
)
from fastapi_standalone_di.singleton import singleton

__version__ = "0.6.0"

__all__ = [
    "AppState",
    "AutoBindingError",
    "Binding",
    "ConflictSolver",
    "CyclicDependencyError",
    "DependantCache",
    "DependencyOverrides",
    "DependencyScope",
    "FastAPIContainer",
    "MissingParameterError",
    "ParamSource",
    "ParameterError",
    "ParameterValidationError",
    "RegistrableDependency",
    "ResolutionScope",
    "ResolvedDependencies",
    "ScopeError",
    "__version__",
    "auto_bindings",
    "container_lifespan",
    "get_app_state",
    "get_container",
    "patch_for_registrable_dependency_support",
    "register_bindings",
    "set_app_state_value",
    "singleton",
]
