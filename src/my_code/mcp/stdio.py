"""Dependency-free legacy MCP JSON-RPC client over a stdio subprocess."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from my_code.foundation.json import JsonObject, JsonValue, to_json_object, to_json_value
from my_code.mcp.models import (
    McpCallResult,
    McpConnectionInfo,
    McpRemoteTool,
    McpServerSpec,
)
from my_code.mcp.transport import (
    McpConfigurationError,
    McpConnectionError,
    McpProtocolError,
    McpRequestError,
)

# 2026-07-28 removes initialize and requires a distinct sessionless driver. Keeping
# this list explicit prevents an accidental partial negotiation with that protocol.
MCP_LEGACY_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_LEGACY_PROTOCOL_VERSIONS = frozenset(
    {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
)

_SAFE_INHERITED_ENVIRONMENT = (
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
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_TOOL_LIST_PAGES = 1_000
_PROCESS_EXIT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class StdioMcpTransportFactory:
    """Resolve env references once without inheriting unrelated process secrets."""

    environ: Mapping[str, str]

    def __call__(self, spec: McpServerSpec) -> StdioMcpTransport:
        environment = {
            name: self.environ[name]
            for name in _SAFE_INHERITED_ENVIRONMENT
            if name in self.environ
        }
        for target, source in spec.env_from:
            try:
                environment[target] = self.environ[source]
            except KeyError as error:
                raise McpConfigurationError(
                    f"Referenced environment variable is missing: {source}"
                ) from error
        return StdioMcpTransport(spec, environment=environment)


class StdioMcpTransport:
    """Multiplex JSON-RPC responses while keeping protocol details inside MCP."""

    def __init__(
        self,
        spec: McpServerSpec,
        *,
        environment: Mapping[str, str],
    ) -> None:
        self.spec = spec
        self._environment = dict(environment)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._next_id = 1
        self._writer_lock = asyncio.Lock()
        self._closing = False
        self._failure: McpConnectionError | McpProtocolError | None = None
        self._server_capabilities: JsonObject = {}
        self._tools_changed_handler: Callable[[], None] | None = None

    def set_tools_changed_handler(self, handler: Callable[[], None] | None) -> None:
        self._tools_changed_handler = handler

    async def connect(self, *, timeout_seconds: float) -> McpConnectionInfo:
        if self._process is not None:
            raise McpConnectionError("MCP stdio transport is already connected")
        try:
            process = await asyncio.create_subprocess_exec(
                self.spec.command,
                *self.spec.args,
                cwd=str(self.spec.cwd),
                env=self._environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_MAX_MESSAGE_BYTES,
            )
        except (OSError, ValueError) as error:
            raise McpConnectionError(
                "MCP server process could not be started"
            ) from error
        self._process = process
        assert process.stdout is not None
        assert process.stderr is not None
        self._reader_task = asyncio.create_task(
            self._read_messages(process.stdout),
            name=f"my-code:mcp:{self.spec.name}:stdout",
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process.stderr),
            name=f"my-code:mcp:{self.spec.name}:stderr",
        )
        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": MCP_LEGACY_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "my-code", "version": "0.1.0"},
                },
                timeout_seconds=timeout_seconds,
            )
            info = self._parse_initialize_result(result)
            await self._notify("notifications/initialized")
            return info
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise

    async def list_tools(self, *, timeout_seconds: float) -> tuple[McpRemoteTool, ...]:
        if "tools" not in self._server_capabilities:
            return ()
        tools: list[McpRemoteTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_TOOL_LIST_PAGES):
            params: JsonObject = {} if cursor is None else {"cursor": cursor}
            result = await self._request(
                "tools/list",
                params,
                timeout_seconds=timeout_seconds,
            )
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise McpProtocolError("MCP tools/list result is invalid")
            for raw in raw_tools:
                if not isinstance(raw, dict):
                    raise McpProtocolError("MCP tool definition is invalid")
                name = raw.get("name")
                description = raw.get("description", "")
                input_schema = raw.get("inputSchema")
                if (
                    not isinstance(name, str)
                    or not isinstance(description, str)
                    or not isinstance(input_schema, dict)
                ):
                    raise McpProtocolError("MCP tool definition is invalid")
                try:
                    tools.append(
                        McpRemoteTool(
                            name,
                            description,
                            to_json_object(input_schema),
                        )
                    )
                except (TypeError, ValueError) as error:
                    raise McpProtocolError("MCP tool definition is invalid") from error
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tuple(tools)
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise McpProtocolError("MCP tools/list cursor is invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise McpProtocolError("MCP tools/list exceeded the pagination limit")

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        timeout_seconds: float,
    ) -> McpCallResult:
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout_seconds=timeout_seconds,
        )
        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise McpProtocolError("MCP tools/call isError must be a boolean")
        return McpCallResult(_render_tool_result(result), is_error=is_error)

    def _parse_initialize_result(self, result: JsonObject) -> McpConnectionInfo:
        version = result.get("protocolVersion")
        capabilities = result.get("capabilities")
        server_info = result.get("serverInfo")
        if (
            not isinstance(version, str)
            or version not in SUPPORTED_LEGACY_PROTOCOL_VERSIONS
            or not isinstance(capabilities, dict)
            or not isinstance(server_info, dict)
        ):
            raise McpProtocolError("MCP initialize result is incompatible")
        name = server_info.get("name")
        server_version = server_info.get("version")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(server_version, str)
            or not server_version.strip()
        ):
            raise McpProtocolError("MCP serverInfo is invalid")
        try:
            self._server_capabilities = to_json_object(capabilities)
        except TypeError as error:
            raise McpProtocolError("MCP capabilities are invalid") from error
        return McpConnectionInfo(version, name, server_version)

    async def _request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float,
    ) -> JsonObject:
        if self._failure is not None:
            raise self._failure
        process = self._process
        if process is None or process.returncode is not None:
            raise McpConnectionError("MCP server is not connected")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            response = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=timeout_seconds,
            )
        except TimeoutError as error:
            await self._cancel_request(request_id, "MCP request timed out")
            raise McpRequestError("timeout") from error
        except asyncio.CancelledError:
            await self._cancel_request(request_id, "MCP request was cancelled")
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
        if "error" in response:
            raw_error = response["error"]
            code: int | str = "remote_error"
            if isinstance(raw_error, dict):
                raw_code = raw_error.get("code")
                if isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool):
                    code = raw_code
            raise McpRequestError(code)
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError("MCP response result must be an object")
        try:
            return to_json_object(result)
        except TypeError as error:
            raise McpProtocolError("MCP response result is not JSON") from error

    async def _cancel_request(self, request_id: int, reason: str) -> None:
        with suppress(McpConnectionError, BrokenPipeError):
            await self._notify(
                "notifications/cancelled",
                {"requestId": request_id, "reason": reason},
            )

    async def _notify(self, method: str, params: JsonObject | None = None) -> None:
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def _send(self, message: JsonObject) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise McpConnectionError("MCP server input is closed")
        try:
            encoded = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > _MAX_MESSAGE_BYTES:
                raise McpProtocolError("MCP outbound message is too large")
            async with self._writer_lock:
                process.stdin.write(encoded + b"\n")
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise McpConnectionError("MCP server input is closed") from error
        except (TypeError, ValueError) as error:
            raise McpProtocolError("MCP outbound message is not valid JSON") from error

    async def _read_messages(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    if not self._closing:
                        self._fail_pending(
                            McpConnectionError("MCP server output was closed")
                        )
                    return
                if len(line) > _MAX_MESSAGE_BYTES:
                    self._fail_pending(McpProtocolError("MCP message is too large"))
                    return
                try:
                    decoded = json.loads(line)
                    message = to_json_object(decoded)
                except (json.JSONDecodeError, UnicodeError, TypeError):
                    self._fail_pending(McpProtocolError("MCP message is invalid"))
                    return
                await self._route_message(message)
        except (ValueError, asyncio.LimitOverrunError):
            self._fail_pending(McpProtocolError("MCP message framing is invalid"))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail_pending(McpConnectionError("MCP reader failed"))

    async def _route_message(self, message: JsonObject) -> None:
        response_id = message.get("id")
        method = message.get("method")
        if method == "notifications/tools/list_changed":
            handler = self._tools_changed_handler
            if handler is not None:
                try:
                    handler()
                except Exception:
                    pass
            return
        if isinstance(response_id, int) and not isinstance(response_id, bool):
            if isinstance(method, str):
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": response_id,
                        "error": {
                            "code": -32601,
                            "message": "Client method is not supported",
                        },
                    }
                )
                return
            pending = self._pending.get(response_id)
            if pending is not None and not pending.done():
                pending.set_result(message)
        # Server notifications are intentionally ignored in static M5a.

    def _fail_pending(self, error: McpConnectionError | McpProtocolError) -> None:
        self._failure = error
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    @staticmethod
    async def _drain_stderr(stream: asyncio.StreamReader) -> None:
        try:
            while await stream.read(64 * 1024):
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._tools_changed_handler = None
        process = self._process
        self._process = None
        self._fail_pending(McpConnectionError("MCP transport is closed"))
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=_PROCESS_EXIT_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=_PROCESS_EXIT_TIMEOUT_SECONDS
                        )
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
        tasks = tuple(
            task for task in (self._reader_task, self._stderr_task) if task is not None
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None


def _render_tool_result(result: JsonObject) -> str:
    rendered: list[str] = []
    content = result.get("content", [])
    if not isinstance(content, list):
        raise McpProtocolError("MCP tools/call content must be an array")
    for block in content:
        if not isinstance(block, dict):
            raise McpProtocolError("MCP tools/call content block is invalid")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            rendered.append(cast(str, block["text"]))
        else:
            rendered.append(_compact_json(block))
    structured = result.get("structuredContent")
    if structured is not None:
        rendered.append(_compact_json(structured))
    return "\n".join(rendered) if rendered else "MCP tool completed with no content."


def _compact_json(value: object) -> str:
    try:
        normalized: JsonValue = to_json_value(value)
    except TypeError as error:
        raise McpProtocolError("MCP tool result is not JSON") from error
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "MCP_LEGACY_PROTOCOL_VERSION",
    "SUPPORTED_LEGACY_PROTOCOL_VERSIONS",
    "StdioMcpTransport",
    "StdioMcpTransportFactory",
]
