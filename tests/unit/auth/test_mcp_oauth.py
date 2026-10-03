"""MCP OAuth 凭据只写入受限的用户私有文件。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from my_code.auth.mcp_oauth import McpOAuthTokenStore


@pytest.mark.asyncio
async def test_oauth_token_store_is_private_and_isolated(tmp_path: Path) -> None:
    root = tmp_path / ".mcp-oauth"
    first = McpOAuthTokenStore(root, "one", "https://example.com/mcp")
    second = McpOAuthTokenStore(root, "two", "https://example.com/mcp")
    token = OAuthToken(access_token="secret", token_type="Bearer")

    assert await first.get_tokens() is None
    await first.set_tokens(token)

    saved = await first.get_tokens()
    assert saved is not None
    assert saved.access_token == "secret"
    assert await second.get_tokens() is None
    await first.set_client_info(
        OAuthClientInformationFull.model_validate(
            {
                "client_id": "registered-client",
                "redirect_uris": ["http://127.0.0.1/callback"],
            }
        )
    )
    registered = await first.get_client_info()
    assert registered is not None
    assert registered.client_id == "registered-client"
    assert (await first.get_tokens()) is not None
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_oauth_store_rejects_symlinked_credential_path(tmp_path: Path) -> None:
    root = tmp_path / ".mcp-oauth"
    root.mkdir()
    store = McpOAuthTokenStore(root, "one", "https://example.com/mcp")
    target = tmp_path / "target"
    target.write_text("secret", encoding="utf-8")
    store.path.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        await store.set_tokens(OAuthToken(access_token="secret", token_type="Bearer"))
    assert target.read_text(encoding="utf-8") == "secret"
