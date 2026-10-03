"""验证 MCP OAuth 本机回调对失败响应和无头模式的处理。"""

from __future__ import annotations

import asyncio

import pytest

from my_code.mcp.oauth import LoopbackOAuthCallback


class _Writer:
    def __init__(self) -> None:
        self.response = b""

    def write(self, data: bytes) -> None:
        self.response += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.mark.asyncio
async def test_oauth_callback_surfaces_authorization_denial() -> None:
    callback = LoopbackOAuthCallback(interactive=True, announce=None)
    callback._result = asyncio.get_running_loop().create_future()
    reader = asyncio.StreamReader()
    reader.feed_data(f"GET {callback._path}?error=access_denied HTTP/1.1\r\n".encode())
    reader.feed_eof()
    writer = _Writer()

    await callback._handle(reader, writer)  # type: ignore[arg-type]

    assert writer.response.startswith(b"HTTP/1.1 200 OK")
    with pytest.raises(RuntimeError, match="denied"):
        await callback.callback()


@pytest.mark.asyncio
async def test_headless_oauth_cannot_start_interactive_redirect() -> None:
    callback = LoopbackOAuthCallback(interactive=False, announce=None)
    with pytest.raises(RuntimeError, match="headless"):
        await callback.redirect("https://example.com/authorize")
