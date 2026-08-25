"""Application-owned MCP connection, discovery, and catalog lifecycle."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from my_code.foundation.json import JsonObject
from my_code.mcp.models import (
    McpCallResult,
    McpConnectionInfo,
    McpConnectionState,
    McpDiagnostic,
    McpDiagnosticCode,
    McpRemoteTool,
    McpSearchMatch,
    McpServerSnapshot,
    McpServerSpec,
    public_tool_name,
)
from my_code.mcp.schema import validate_tool_schema
from my_code.mcp.search_tool import McpToolSearch
from my_code.mcp.tool import McpTool
from my_code.mcp.transport import (
    McpConfigurationError,
    McpConnectionError,
    McpProtocolError,
    McpRequestError,
    McpTransport,
    McpTransportError,
    McpTransportFactory,
)
from my_code.tools.base import Tool, ToolExecutionError
from my_code.tools.catalog import ToolCatalog, ToolSourceId

DEFAULT_DEFERRED_TOOL_THRESHOLD = 50


class McpRuntimeError(ToolExecutionError):
    """A stable execution error that contains no command, env value, or stderr."""


@dataclass(slots=True)
class _ServerRuntime:
    spec: McpServerSpec
    state: McpConnectionState
    transport: McpTransport | None = None
    info: McpConnectionInfo | None = None
    remote_tools: tuple[McpRemoteTool, ...] = ()
    activated_remote_names: frozenset[str] = frozenset()
    tool_names: tuple[str, ...] = ()
    diagnostic: McpDiagnostic | None = None


class McpRuntime:
    """Own every configured server and publish each complete source atomically."""

    def __init__(
        self,
        *,
        enabled: bool,
        servers: tuple[McpServerSpec, ...],
        catalog: ToolCatalog,
        transport_factory: McpTransportFactory,
        deferred_tool_threshold: int = DEFAULT_DEFERRED_TOOL_THRESHOLD,
    ) -> None:
        names = [server.name for server in servers]
        if len(names) != len(set(names)):
            raise ValueError(
                "MCP server names must be unique after settings resolution"
            )
        if deferred_tool_threshold <= 0:
            raise ValueError("MCP deferred tool threshold must be positive")
        self.enabled = enabled
        self.catalog = catalog
        self.deferred_tool_threshold = deferred_tool_threshold
        self._transport_factory = transport_factory
        self._servers = {
            spec.name: _ServerRuntime(spec, self._initial_state(spec))
            for spec in sorted(servers, key=lambda item: item.name)
        }
        self._lock = asyncio.Lock()
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._refresh_requested: set[str] = set()
        self._started = False
        self._closed = False

    def _initial_state(self, spec: McpServerSpec) -> McpConnectionState:
        if not self.enabled or not spec.enabled or not spec.start_allowed:
            return McpConnectionState.DISABLED
        return McpConnectionState.PENDING

    @property
    def started(self) -> bool:
        return self._started

    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return tuple(
            self._snapshot(name, server) for name, server in self._servers.items()
        )

    def snapshot(self, server_name: str) -> McpServerSnapshot:
        try:
            server = self._servers[server_name]
        except KeyError as error:
            raise ValueError(f"Unknown MCP server: {server_name}") from error
        return self._snapshot(server_name, server)

    @staticmethod
    def _snapshot(name: str, server: _ServerRuntime) -> McpServerSnapshot:
        return McpServerSnapshot(
            name=name,
            state=server.state,
            tool_names=server.tool_names,
            diagnostic=server.diagnostic,
            connection_info=server.info,
        )

    async def start(self) -> None:
        async with self._lock:
            if self._closed or self._started:
                return
            self._started = True
            for server in self._servers.values():
                if not self.enabled:
                    self._disable(
                        server,
                        McpDiagnosticCode.GATE_DISABLED,
                        "MCP is disabled by the feature gate.",
                    )
                elif not server.spec.enabled:
                    self._disable(
                        server,
                        McpDiagnosticCode.SERVER_DISABLED,
                        "MCP server is disabled by settings.",
                    )
                elif not server.spec.start_allowed:
                    self._disable(
                        server,
                        McpDiagnosticCode.PROJECT_NOT_TRUSTED,
                        "Shared project MCP server requires a local trusted copy.",
                    )
                else:
                    await self._connect(server)

    async def reconnect(self, server_name: str) -> McpServerSnapshot:
        async with self._lock:
            if self._closed:
                raise McpRuntimeError("MCP runtime is closed")
            server = self._server(server_name)
            await self._detach(server, target=McpConnectionState.PENDING)
            if (
                not self.enabled
                or not server.spec.enabled
                or not server.spec.start_allowed
            ):
                self._disable(
                    server,
                    McpDiagnosticCode.SERVER_DISABLED,
                    "MCP server cannot be started under the current settings.",
                )
            else:
                await self._connect(server)
            return self._snapshot(server_name, server)

    async def disconnect(self, server_name: str) -> None:
        async with self._lock:
            server = self._server(server_name)
            await self._detach(server, target=McpConnectionState.FAILED)
            server.diagnostic = McpDiagnostic(
                server.spec.name,
                server.state,
                McpDiagnosticCode.CONNECTION_LOST,
                "MCP server was disconnected.",
            )

    async def refresh(self, server_name: str) -> McpServerSnapshot:
        """Atomically publish a changed list; invalid refresh keeps the old source."""

        async with self._lock:
            if self._closed:
                raise McpRuntimeError("MCP runtime is closed")
            server = self._server(server_name)
            if (
                server.state is not McpConnectionState.CONNECTED
                or server.transport is None
            ):
                raise McpRuntimeError(f"MCP server {server_name!r} is unavailable")
            try:
                normalized = await self._discover(server)
            except asyncio.CancelledError:
                raise
            except McpConnectionError:
                await self._detach(server, target=McpConnectionState.FAILED)
                server.diagnostic = McpDiagnostic(
                    server.spec.name,
                    server.state,
                    McpDiagnosticCode.CONNECTION_LOST,
                    "MCP connection was lost during tool refresh.",
                )
                return self._snapshot(server_name, server)
            except (ValueError, TypeError, McpTransportError):
                server.diagnostic = McpDiagnostic(
                    server.spec.name,
                    server.state,
                    McpDiagnosticCode.DISCOVERY_FAILED,
                    "MCP tool refresh failed; the previous source remains active.",
                )
                return self._snapshot(server_name, server)
            except Exception:
                server.diagnostic = McpDiagnostic(
                    server.spec.name,
                    server.state,
                    McpDiagnosticCode.DISCOVERY_FAILED,
                    "MCP tool refresh failed unexpectedly; the previous source "
                    "remains active.",
                )
                return self._snapshot(server_name, server)
            if normalized == server.remote_tools:
                server.diagnostic = None
                return self._snapshot(server_name, server)
            retained = frozenset(
                name
                for name in server.activated_remote_names
                if any(remote.name == name for remote in normalized)
            )
            tools = self._published_tools(server, normalized, retained)
            try:
                self.catalog.replace_source(self._source(server), tools)
            except (ValueError, TypeError):
                server.diagnostic = McpDiagnostic(
                    server.spec.name,
                    server.state,
                    McpDiagnosticCode.REGISTRATION_FAILED,
                    "MCP refreshed tools conflict with the active catalog.",
                )
                return self._snapshot(server_name, server)
            self._commit_tools(server, normalized, retained, tools)
            return self._snapshot(server_name, server)

    async def wait_for_refreshes(self) -> None:
        """Wait until all currently/coalesced notification refreshes are terminal."""

        while self._refresh_tasks:
            await asyncio.gather(*tuple(self._refresh_tasks), return_exceptions=True)

    async def search_and_activate(
        self,
        server_name: str,
        query: str,
        *,
        limit: int,
    ) -> tuple[McpSearchMatch, ...]:
        async with self._lock:
            if self._closed:
                raise McpRuntimeError("MCP runtime is closed")
            server = self._server(server_name)
            if server.state is not McpConnectionState.CONNECTED:
                raise McpRuntimeError(f"MCP server {server_name!r} is unavailable")
            if not self._is_deferred(server.remote_tools):
                raise McpRuntimeError(
                    f"MCP server {server_name!r} does not use deferred tools"
                )
            matches = _search(server.spec.name, server.remote_tools, query, limit)
            activated = server.activated_remote_names | frozenset(
                match.remote_name for match in matches
            )
            if activated != server.activated_remote_names:
                tools = self._published_tools(server, server.remote_tools, activated)
                try:
                    self.catalog.replace_source(self._source(server), tools)
                except (ValueError, TypeError) as error:
                    raise McpRuntimeError(
                        f"MCP server {server_name!r} tools could not be activated"
                    ) from error
                self._commit_tools(server, server.remote_tools, activated, tools)
            return matches

    async def call_tool(
        self, server_name: str, remote_name: str, arguments: JsonObject
    ) -> McpCallResult:
        server = self._servers.get(server_name)
        if (
            server is None
            or server.state is not McpConnectionState.CONNECTED
            or server.transport is None
        ):
            raise McpRuntimeError(f"MCP server {server_name!r} is unavailable")
        try:
            return await server.transport.call_tool(
                remote_name,
                arguments,
                timeout_seconds=server.spec.call_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except McpConnectionError as error:
            await self._connection_lost(server)
            raise McpRuntimeError(
                f"MCP server {server_name!r} disconnected during tool call"
            ) from error
        except (McpRequestError, McpProtocolError, McpTransportError) as error:
            raise McpRuntimeError(
                f"MCP server {server_name!r} could not complete the tool call"
            ) from error
        except Exception as error:
            raise McpRuntimeError(
                f"MCP server {server_name!r} failed unexpectedly"
            ) from error

    async def _connect(self, server: _ServerRuntime) -> None:
        server.state = McpConnectionState.PENDING
        server.diagnostic = None
        try:
            transport = self._transport_factory(server.spec)
        except McpConfigurationError:
            self._fail(
                server,
                McpDiagnosticCode.CONFIGURATION_ERROR,
                "MCP server environment references could not be resolved.",
            )
            return
        except Exception:
            self._fail(
                server,
                McpDiagnosticCode.START_FAILED,
                "MCP transport could not be created.",
            )
            return
        server.transport = transport
        transport.set_tools_changed_handler(
            lambda: self._schedule_refresh(server.spec.name)
        )
        try:
            info = await transport.connect(
                timeout_seconds=server.spec.startup_timeout_seconds
            )
        except asyncio.CancelledError:
            await self._close_failed_transport(server)
            server.state = McpConnectionState.FAILED
            raise
        except Exception:
            await self._close_failed_transport(server)
            self._fail(
                server,
                McpDiagnosticCode.START_FAILED,
                "MCP server failed to initialize.",
            )
            return
        try:
            normalized = await self._discover(server)
            tools = self._published_tools(server, normalized, frozenset())
            self.catalog.register_source(self._source(server), tools)
        except asyncio.CancelledError:
            await self._close_failed_transport(server)
            server.state = McpConnectionState.FAILED
            raise
        except (ValueError, McpTransportError, TypeError):
            await self._close_failed_transport(server)
            self._fail(
                server,
                McpDiagnosticCode.DISCOVERY_FAILED,
                "MCP tool discovery or schema validation failed.",
            )
            return
        except Exception:
            await self._close_failed_transport(server)
            self._fail(
                server,
                McpDiagnosticCode.REGISTRATION_FAILED,
                "MCP tools could not be registered.",
            )
            return
        server.info = info
        server.state = McpConnectionState.CONNECTED
        self._commit_tools(server, normalized, frozenset(), tools)

    async def _discover(self, server: _ServerRuntime) -> tuple[McpRemoteTool, ...]:
        assert server.transport is not None
        discovered = await server.transport.list_tools(
            timeout_seconds=server.spec.startup_timeout_seconds
        )
        normalized = tuple(
            McpRemoteTool(
                remote.name,
                remote.description,
                validate_tool_schema(remote.input_schema),
            )
            for remote in discovered
        )
        public_names = tuple(
            public_tool_name(server.spec.name, remote.name) for remote in normalized
        )
        if len(public_names) != len(set(public_names)):
            raise ValueError("MCP tool names collide after namespace normalization")
        return normalized

    def _published_tools(
        self,
        server: _ServerRuntime,
        remote_tools: tuple[McpRemoteTool, ...],
        activated: frozenset[str],
    ) -> tuple[Tool, ...]:
        if not self._is_deferred(remote_tools):
            exposed = remote_tools
            search: tuple[Tool, ...] = ()
        else:
            exposed = tuple(
                remote for remote in remote_tools if remote.name in activated
            )
            search = (
                McpToolSearch(
                    self,
                    server_name=server.spec.name,
                    tool_count=len(remote_tools),
                ),
            )
        return (
            *search,
            *(
                McpTool(self, server_name=server.spec.name, remote=remote)
                for remote in exposed
            ),
        )

    def _commit_tools(
        self,
        server: _ServerRuntime,
        remote_tools: tuple[McpRemoteTool, ...],
        activated: frozenset[str],
        tools: tuple[Tool, ...],
    ) -> None:
        server.remote_tools = remote_tools
        server.activated_remote_names = (
            activated if self._is_deferred(remote_tools) else frozenset()
        )
        server.tool_names = tuple(tool.definition.name for tool in tools)
        server.diagnostic = None

    def _is_deferred(self, tools: tuple[McpRemoteTool, ...]) -> bool:
        return len(tools) > self.deferred_tool_threshold

    def _schedule_refresh(self, server_name: str) -> None:
        if self._closed:
            return
        self._refresh_requested.add(server_name)
        if any(
            not task.done() and task.get_name() == self._refresh_task_name(server_name)
            for task in self._refresh_tasks
        ):
            return
        task = asyncio.create_task(
            self._refresh_until_clean(server_name),
            name=self._refresh_task_name(server_name),
        )
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_done)

    async def _refresh_until_clean(self, server_name: str) -> None:
        while server_name in self._refresh_requested and not self._closed:
            self._refresh_requested.discard(server_name)
            try:
                await self.refresh(server_name)
            except McpRuntimeError:
                return

    def _refresh_done(self, task: asyncio.Task[None]) -> None:
        self._refresh_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            pass

    @staticmethod
    def _refresh_task_name(server_name: str) -> str:
        return f"my-code:mcp:{server_name}:refresh"

    async def _connection_lost(self, server: _ServerRuntime) -> None:
        async with self._lock:
            if server.state is not McpConnectionState.CONNECTED:
                return
            await self._detach(server, target=McpConnectionState.FAILED)
            server.diagnostic = McpDiagnostic(
                server.spec.name,
                server.state,
                McpDiagnosticCode.CONNECTION_LOST,
                "MCP connection was lost.",
            )

    async def _detach(
        self, server: _ServerRuntime, *, target: McpConnectionState
    ) -> None:
        self.catalog.unregister_source(self._source(server))
        transport = server.transport
        server.transport = None
        server.info = None
        server.remote_tools = ()
        server.activated_remote_names = frozenset()
        server.tool_names = ()
        server.state = target
        server.diagnostic = None
        if transport is not None:
            transport.set_tools_changed_handler(None)
            try:
                await transport.close()
            except Exception:
                pass

    async def _close_failed_transport(self, server: _ServerRuntime) -> None:
        transport = server.transport
        server.transport = None
        if transport is not None:
            transport.set_tools_changed_handler(None)
            try:
                await transport.close()
            except Exception:
                pass

    def _disable(
        self,
        server: _ServerRuntime,
        code: McpDiagnosticCode,
        message: str,
    ) -> None:
        server.state = McpConnectionState.DISABLED
        server.diagnostic = McpDiagnostic(server.spec.name, server.state, code, message)

    def _fail(
        self,
        server: _ServerRuntime,
        code: McpDiagnosticCode,
        message: str,
    ) -> None:
        self.catalog.unregister_source(self._source(server))
        server.info = None
        server.remote_tools = ()
        server.activated_remote_names = frozenset()
        server.tool_names = ()
        server.state = McpConnectionState.FAILED
        server.diagnostic = McpDiagnostic(server.spec.name, server.state, code, message)

    def _server(self, server_name: str) -> _ServerRuntime:
        try:
            return self._servers[server_name]
        except KeyError as error:
            raise ValueError(f"Unknown MCP server: {server_name}") from error

    @staticmethod
    def _source(server: _ServerRuntime) -> ToolSourceId:
        return ToolSourceId("mcp", server.spec.name)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._refresh_requested.clear()
        refreshes = tuple(self._refresh_tasks)
        for task in refreshes:
            if not task.done():
                task.cancel()
        if refreshes:
            await asyncio.gather(*refreshes, return_exceptions=True)
        async with self._lock:
            errors: list[Exception] = []
            for server in self._servers.values():
                self.catalog.unregister_source(self._source(server))
                transport = server.transport
                server.transport = None
                server.info = None
                server.remote_tools = ()
                server.activated_remote_names = frozenset()
                server.tool_names = ()
                server.state = McpConnectionState.CLOSED
                server.diagnostic = None
                if transport is not None:
                    transport.set_tools_changed_handler(None)
                    try:
                        await transport.close()
                    except Exception as error:
                        errors.append(error)
            if errors:
                raise ExceptionGroup("Failed to close MCP runtime", errors)


def _search(
    server_name: str,
    tools: tuple[McpRemoteTool, ...],
    query: str,
    limit: int,
) -> tuple[McpSearchMatch, ...]:
    normalized_query = query.casefold().strip()
    if not normalized_query:
        return ()
    terms = tuple(term for term in re.split(r"[^a-z0-9]+", normalized_query) if term)
    scored: list[tuple[int, str, McpRemoteTool]] = []
    for remote in tools:
        name = remote.name.casefold()
        description = remote.description.casefold()
        score = 0
        if normalized_query == name:
            score += 1_000
        elif normalized_query in name:
            score += 500
        elif normalized_query in description:
            score += 100
        for term in terms:
            if term in name:
                score += 20
            if term in description:
                score += 5
        if score:
            scored.append((score, remote.name, remote))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(
        McpSearchMatch(
            server_name,
            remote.name,
            public_tool_name(server_name, remote.name),
            remote.description,
        )
        for _, _, remote in scored[:limit]
    )


__all__ = [
    "DEFAULT_DEFERRED_TOOL_THRESHOLD",
    "McpRuntime",
    "McpRuntimeError",
]
