"""交互式 MCP OAuth 的本地回调入口。"""

from __future__ import annotations

import asyncio
import secrets
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from mcp.shared.auth import AuthorizationCodeResult


class LoopbackOAuthCallback:
    def __init__(
        self, *, interactive: bool, announce: Callable[[str], Awaitable[None]] | None
    ) -> None:
        self.interactive = interactive
        self.announce = announce
        self._path = f"/mcp-oauth/{secrets.token_urlsafe(16)}"
        self._server: asyncio.AbstractServer | None = None
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None
        self.redirect_uri = ""

    async def __aenter__(self) -> LoopbackOAuthCallback:
        if not self.interactive:
            return self
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.redirect_uri = f"http://127.0.0.1:{port}{self._path}"
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._result is not None and not self._result.done():
            self._result.cancel()

    async def redirect(self, url: str) -> None:
        if not self.interactive:
            raise RuntimeError("Interactive MCP OAuth is unavailable in headless mode")
        if self.announce is not None:
            await self.announce(url)
        if not await asyncio.to_thread(webbrowser.open, url) and self.announce is None:
            print(f"Open this MCP authorization URL: {url}", file=sys.stderr)

    async def callback(self) -> AuthorizationCodeResult:
        if self._result is None:
            raise RuntimeError("Interactive MCP OAuth is unavailable in headless mode")
        return await asyncio.wait_for(self._result, timeout=180)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = line.decode("ascii", errors="replace").split(" ")
            target = urlsplit(parts[1]) if len(parts) >= 2 else None
            valid = (
                parts[0] == "GET" and target is not None and target.path == self._path
            )
            if valid and self._result is not None and not self._result.done():
                assert target is not None
                query = parse_qs(target.query)
                code = query.get("code", [""])[0]
                if code:
                    self._result.set_result(
                        AuthorizationCodeResult(
                            code=code,
                            state=query.get("state", [None])[0],
                            iss=query.get("iss", [None])[0],
                        )
                    )
                elif query.get("error"):
                    self._result.set_exception(
                        RuntimeError("MCP OAuth authorization was denied")
                    )
            body = b"MCP authorization received. You can close this page."
            status = b"200 OK" if valid else b"404 Not Found"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: text/plain\r\n"
                + b"Content-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


__all__ = ["LoopbackOAuthCallback"]
