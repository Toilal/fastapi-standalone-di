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


async def test_security_scopes_empty_by_default() -> None:
    async with FastAPIContainer() as container:
        result = await container.get(wants_scopes)
    assert isinstance(result, SecurityScopes)
    assert result.scopes == []
    assert result.scope_str == ""


async def test_security_scopes_from_container_config() -> None:
    async with FastAPIContainer(security_scopes=["me", "items"]) as container:
        result = await container.get(wants_scopes)
    assert result.scopes == ["me", "items"]
    assert result.scope_str == "me items"


async def test_configured_scopes_reach_dependency_behind_security() -> None:
    async with FastAPIContainer(security_scopes=["me"]) as container:
        result = await container.invoke(root_over_security)
    # Scopes come from the container config, not from the parent's
    # ``Security(..., scopes=["items"])`` marker: standalone has no chain to
    # accumulate, so the configured scopes apply uniformly across the tree.
    assert result == ["me"]
