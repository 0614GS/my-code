"""MCP Bearer 凭据不进入 settings，并隔离不同端点。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from my_code.auth.mcp_bearer import McpBearerTokenStore


def test_mcp_bearer_store_is_private_isolated_and_removable(tmp_path: Path) -> None:
    root = tmp_path / ".mcp-bearer"
    first = McpBearerTokenStore(root, "one", "https://example.com/mcp")
    second = McpBearerTokenStore(root, "two", "https://example.com/mcp")
    assert first.load() is None
    first.save("secret")
    assert first.load() == "secret"
    assert second.load() is None
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    assert first.delete()
    assert first.load() is None


def test_mcp_bearer_store_rejects_symlink_and_invalid_token(tmp_path: Path) -> None:
    root = tmp_path / ".mcp-bearer"
    root.mkdir()
    store = McpBearerTokenStore(root, "one", "https://example.com/mcp")
    target = tmp_path / "target"
    target.write_text("original", encoding="utf-8")
    store.path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        store.save("secret")
    with pytest.raises(ValueError, match="symlink"):
        store.load()
    with pytest.raises(ValueError, match="symlink"):
        store.delete()
    assert target.read_text(encoding="utf-8") == "original"
    store.path.unlink()
    with pytest.raises(ValueError, match="without whitespace"):
        store.save("invalid token")
