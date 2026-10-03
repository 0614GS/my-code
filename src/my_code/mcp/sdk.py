"""使用官方 SDK 承载 MCP 连接，向应用只暴露本地域模型。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import ToolsListChanged
from mcp.shared.auth import OAuthClientMetadata
from mcp.types import CallToolResult, TextContent, ToolListChangedNotification

from my_code.auth.mcp_oauth import McpOAuthTokenStore
from my_code.foundation.json import JsonObject, to_json_object
from my_code.mcp.models import (
    McpAuthKind,
    McpCallResult,
    McpConnectionInfo,
    McpRemoteTool,
    McpServerSpec,
    McpServerTransport,
)
from my_code.mcp.oauth import LoopbackOAuthCallback
from my_code.mcp.transport import (
    McpConfigurationError,
    McpConnectionError,
    McpProtocolError,
    McpRequestError,
)

_SAFE_ENV = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
)
_MAX_PAGES = 1000


class SdkMcpTransportFactory:
    def __init__(
        self,
        environ: Mapping[str, str],
        oauth_root: Path,
        *,
        interactive: bool,
        announce_auth: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.environ = environ
        self.oauth_root = oauth_root
        self.interactive = interactive
        self.announce_auth = announce_auth

    def __call__(self, spec: McpServerSpec) -> SdkMcpTransport:
        environment = {
            name: self.environ[name] for name in _SAFE_ENV if name in self.environ
        }
        for target, source in spec.env_from:
            if source not in self.environ:
                raise McpConfigurationError(
                    f"Referenced environment variable is missing: {source}"
                )
            environment[target] = self.environ[source]
        token = None
        if spec.auth is McpAuthKind.BEARER:
            source = spec.bearer_token_from
            if source is None or not self.environ.get(source):
                raise McpConfigurationError("MCP Bearer token source is missing")
            token = self.environ[source]
        return SdkMcpTransport(
            spec,
            environment,
            token,
            self.oauth_root,
            interactive=self.interactive,
            announce_auth=self.announce_auth,
        )


class SdkMcpTransport:
    def __init__(
        self,
        spec: McpServerSpec,
        environment: dict[str, str],
        bearer_token: str | None,
        oauth_root: Path,
        *,
        interactive: bool,
        announce_auth: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        self.spec = spec
        self._environment = environment
        self._bearer_token = bearer_token
        self._oauth_root = oauth_root
        self._interactive = interactive
        self._announce_auth = announce_auth
        self._handler: Callable[[], None] | None = None
        self._commands: asyncio.Queue[
            tuple[str, tuple[Any, ...], asyncio.Future[Any]] | None
        ] = asyncio.Queue()
        self._actor: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[McpConnectionInfo] | None = None

    def set_tools_changed_handler(self, handler: Callable[[], None] | None) -> None:
        self._handler = handler

    async def connect(self, *, timeout_seconds: float) -> McpConnectionInfo:
        if self._actor is not None:
            raise McpConnectionError("MCP connection already started")
        self._ready = asyncio.get_running_loop().create_future()
        self._actor = asyncio.create_task(
            self._run(), name=f"my-code:mcp:{self.spec.name}:sdk"
        )
        try:
            limit = (
                max(timeout_seconds, 180)
                if self.spec.auth is McpAuthKind.OAUTH and self._interactive
                else timeout_seconds
            )
            return await asyncio.wait_for(asyncio.shield(self._ready), timeout=limit)
        except BaseException:
            if self._actor is not None:
                self._actor.cancel()
            await self.close()
            raise

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                callback = (
                    await stack.enter_async_context(
                        LoopbackOAuthCallback(
                            interactive=self._interactive, announce=self._announce_auth
                        )
                    )
                    if self.spec.auth is McpAuthKind.OAUTH
                    else None
                )
                if self.spec.transport is McpServerTransport.STDIO:
                    assert self.spec.command is not None
                    server = StdioServerParameters(
                        command=self.spec.command,
                        args=list(self.spec.args),
                        cwd=self.spec.cwd,
                        env=self._environment,
                    )
                    client = await stack.enter_async_context(
                        Client(server, cache=None, message_handler=self._message)
                    )
                else:
                    assert self.spec.url is not None
                    auth: Any = None
                    headers: dict[str, str] = {}
                    if self.spec.auth is McpAuthKind.BEARER:
                        headers["Authorization"] = f"Bearer {self._bearer_token}"
                    elif callback is not None:
                        auth = OAuthClientProvider(
                            self.spec.url,
                            OAuthClientMetadata.model_validate(
                                {
                                    "client_name": "my-code",
                                    "redirect_uris": [
                                        callback.redirect_uri
                                        or "http://127.0.0.1:1/mcp-oauth/headless"
                                    ],
                                }
                            ),
                            McpOAuthTokenStore(
                                self._oauth_root, self.spec.name, self.spec.url
                            ),
                            redirect_handler=callback.redirect,
                            callback_handler=callback.callback,
                        )
                    http = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers=headers,
                            auth=auth,
                            timeout=httpx2.Timeout(
                                30, read=max(300, self.spec.call_timeout_seconds)
                            ),
                        )
                    )
                    transport = streamable_http_client(self.spec.url, http_client=http)
                    client = await stack.enter_async_context(
                        Client(transport, cache=None, message_handler=self._message)
                    )
                info = client.server_info
                assert self._ready is not None
                self._ready.set_result(
                    McpConnectionInfo(
                        client.protocol_version,
                        info.name if info else self.spec.name,
                        info.version if info else "unknown",
                    )
                )
                listener: asyncio.Task[None] | None = None
                if (
                    client.protocol_version >= "2026-07-28"
                    and client.server_capabilities.tools
                    and client.server_capabilities.tools.list_changed
                ):
                    listener = asyncio.create_task(self._listen(client))
                running: set[asyncio.Task[None]] = set()
                try:
                    while (command := await self._commands.get()) is not None:
                        task = asyncio.create_task(self._perform(client, command))
                        command[2].add_done_callback(
                            lambda future, running_task=task: (
                                running_task.cancel()
                                if future.cancelled() and not running_task.done()
                                else None
                            )
                        )
                        running.add(task)
                        task.add_done_callback(running.discard)
                finally:
                    for task in running:
                        task.cancel()
                    if running:
                        await asyncio.gather(*running, return_exceptions=True)
                    if listener is not None:
                        listener.cancel()
                        await asyncio.gather(listener, return_exceptions=True)
        except BaseException as error:
            if self._ready is not None and not self._ready.done():
                if isinstance(error, asyncio.CancelledError):
                    self._ready.cancel()
                else:
                    self._ready.set_exception(
                        McpConnectionError(f"MCP SDK connection failed: {error}")
                    )
            while not self._commands.empty():
                pending = self._commands.get_nowait()
                if pending is not None and not pending[2].done():
                    pending[2].set_exception(
                        McpConnectionError("MCP SDK connection closed")
                    )
            if isinstance(error, asyncio.CancelledError):
                raise

    async def _message(self, message: object) -> None:
        if (
            isinstance(message, ToolListChangedNotification)
            and self._handler is not None
        ):
            self._handler()

    async def _listen(self, client: Client) -> None:
        try:
            async with client.listen(tools_list_changed=True) as stream:
                async for event in stream:
                    if (
                        isinstance(event, ToolsListChanged)
                        and self._handler is not None
                    ):
                        self._handler()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _perform(
        self, client: Client, command: tuple[str, tuple[Any, ...], asyncio.Future[Any]]
    ) -> None:
        operation, arguments, future = command
        if future.cancelled():
            return
        try:
            if operation == "list":
                result = await self._list(client)
            else:
                result = await self._call(client, arguments[0], arguments[1])
            if not future.done():
                future.set_result(result)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as error:
            if not future.done():
                future.set_exception(error)

    async def _list(self, client: Client) -> tuple[McpRemoteTool, ...]:
        found: list[McpRemoteTool] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = await client.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name in seen:
                    raise McpProtocolError("MCP tool names are duplicated")
                seen.add(tool.name)
                found.append(
                    McpRemoteTool(
                        tool.name,
                        tool.description or tool.title or "",
                        to_json_object(tool.input_schema),
                    )
                )
            if page.next_cursor is None:
                return tuple(found)
            cursor = page.next_cursor
        raise McpProtocolError("MCP tools/list exceeded the pagination limit")

    async def _call(
        self, client: Client, name: str, arguments: JsonObject
    ) -> McpCallResult:
        result = await client.session.call_tool(
            name, arguments, allow_input_required=False
        )
        if not isinstance(result, CallToolResult):
            raise McpProtocolError("Unsupported MCP tool result")
        rendered = [
            item.text
            if isinstance(item, TextContent)
            else f"[MCP {item.type} content omitted]"
            for item in result.content
        ]
        if result.structured_content is not None:
            rendered.append(
                json.dumps(
                    result.structured_content, ensure_ascii=False, separators=(",", ":")
                )
            )
        return McpCallResult(
            "\n".join(rendered) or "MCP tool completed with no content.",
            bool(result.is_error),
        )

    async def _ask(
        self, operation: str, arguments: tuple[Any, ...], timeout_seconds: float
    ) -> Any:
        if self._actor is None or self._actor.done():
            raise McpConnectionError("MCP server is not connected")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._commands.put((operation, arguments, future))
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as error:
            raise McpRequestError("timeout") from error

    async def list_tools(self, *, timeout_seconds: float) -> tuple[McpRemoteTool, ...]:
        return await self._ask("list", (), timeout_seconds)

    async def call_tool(
        self, name: str, arguments: JsonObject, *, timeout_seconds: float
    ) -> McpCallResult:
        return await self._ask("call", (name, arguments), timeout_seconds)

    async def close(self) -> None:
        actor = self._actor
        self._actor = None
        self._handler = None
        if actor is not None:
            await self._commands.put(None)
            if not actor.done():
                try:
                    await asyncio.wait_for(actor, timeout=10)
                except (asyncio.CancelledError, TimeoutError):
                    actor.cancel()
                    await asyncio.gather(actor, return_exceptions=True)


__all__ = ["SdkMcpTransport", "SdkMcpTransportFactory"]
