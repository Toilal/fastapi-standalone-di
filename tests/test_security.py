"""Tests for standalone injection of ``SecurityScopes``."""

from fastapi import Security
from fastapi.security import SecurityScopes

from fastapi_standalone_di import FastAPIContainer


def wants_scopes(scopes: SecurityScopes) -> SecurityScopes:
    return scopes


def secured_leaf(scopes: SecurityScopes) -> list[str]:
    return list(scopes.scopes)


def root_over_security(inner: list[str] = Security(secured_leaf, scopes=["items"])):
    return inner


async def test_security_scopes_injected_for_root_dependency() -> None:
    async with FastAPIContainer() as container:
        result = await container.get(wants_scopes)
    assert isinstance(result, SecurityScopes)
    assert result.scopes == []
    assert result.scope_str == ""


async def test_dependency_behind_security_resolves() -> None:
    async with FastAPIContainer() as container:
        result = await container.invoke(root_over_security)
    # The cumulative ``["items"]`` scope a parent declares via ``Security(...)``
    # is not propagated standalone: the re-introspected leaf sees only its own
    # (empty) scopes.
    assert result == []
