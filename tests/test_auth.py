"""Auth layer (issue #7): token → scope set, bound at connect time."""

from pathlib import Path

import pytest
from test_scopes import (
    GROUP_SHARED_IDS,
    GROWTH_ONLY_IDS,
    PLATFORM_ONLY_IDS,
    PUBLIC_IDS,
    RESTRICTED_IDS,
)

from okf_mcp.auth import ANONYMOUS, AuthError, StaticTokenAuthenticator
from okf_mcp.index import OkfIndex
from okf_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_CONFIG = REPO_ROOT / "config" / "auth.yaml"
BUNDLES = (
    REPO_ROOT / "bundles" / "acme-knowledge",
    REPO_ROOT / "bundles" / "acme-knowledge-restricted",
)

ALL_INTERNAL = PUBLIC_IDS | GROWTH_ONLY_IDS | PLATFORM_ONLY_IDS | GROUP_SHARED_IDS

# What each persona token must see — exactly (issue #7 visibility matrix).
PERSONA_MATRIX = {
    "demo-token-a": PUBLIC_IDS | GROWTH_ONLY_IDS | GROUP_SHARED_IDS,
    "demo-token-b": PUBLIC_IDS | PLATFORM_ONLY_IDS | GROUP_SHARED_IDS,
    "demo-token-ab": ALL_INTERNAL,
    "demo-token-c": PUBLIC_IDS,  # unrelated group scope grants nothing extra
    "demo-token-exco": ALL_INTERNAL | RESTRICTED_IDS,
}


@pytest.fixture(scope="module")
def authenticator() -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator.from_file(AUTH_CONFIG)


@pytest.fixture(scope="module")
def catalog() -> OkfIndex:
    return OkfIndex(*BUNDLES)


def test_static_config_resolves_personas(authenticator: StaticTokenAuthenticator) -> None:
    principal = authenticator.authenticate("demo-token-ab")
    assert principal.subject == "user-ab@acme.test"
    assert principal.scopes == {"growth", "platform"}


def test_no_token_is_anonymous(authenticator: StaticTokenAuthenticator) -> None:
    assert authenticator.authenticate(None) == ANONYMOUS


def test_unknown_token_fails_closed(authenticator: StaticTokenAuthenticator) -> None:
    with pytest.raises(AuthError):
        authenticator.authenticate("demo-token-forged")


def test_unauthenticated_caller_sees_public_layer_only(
    authenticator: StaticTokenAuthenticator, catalog: OkfIndex
) -> None:
    scopes = authenticator.authenticate(None).scopes
    assert set(catalog.visible_to(scopes).ids()) == PUBLIC_IDS


def test_persona_visibility_matrix(
    authenticator: StaticTokenAuthenticator, catalog: OkfIndex
) -> None:
    for token, expected in PERSONA_MATRIX.items():
        scopes = authenticator.authenticate(token).scopes
        assert set(catalog.visible_to(scopes).ids()) == expected, token


@pytest.mark.anyio
async def test_no_tool_accepts_scopes_as_input() -> None:
    # The prompt-injection guarantee: scope sets bind at connect time and no
    # tool parameter can name, carry, or widen them.
    server = build_server(BUNDLES, token="demo-token-a")
    for tool in await server.list_tools():
        for param in tool.inputSchema.get("properties", {}):
            assert "scope" not in param.lower(), f"{tool.name}.{param}"
            assert "token" not in param.lower(), f"{tool.name}.{param}"


@pytest.mark.anyio
async def test_server_binds_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKF_TOKEN", "demo-token-exco")
    server = build_server(BUNDLES)
    result = await server.call_tool(
        "get_concept", {"concept_id": "/methods/churn-propensity-model"}
    )
    assert "Churn Propensity" in result[0].text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
