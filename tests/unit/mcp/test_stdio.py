"""Offline stdio protocol checks for framing, handshake, cancellation, and close."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from my_code.mcp.models import McpServerSpec
from my_code.mcp.stdio import StdioMcpTransport, StdioMcpTransportFactory

_SERVER = r"""
import json
import os
import pathlib
import sys

cancel_path = pathlib.Path(sys.argv[1])
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "offline-fake", "version": "1.0"},
            },
        }
    elif method == "tools/list":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [{
                    "name": "echo",
                    "description": "Echo input",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                }]
            },
        }
    elif method == "tools/call" and message["params"]["name"] == "block":
        continue
    elif method == "tools/call":
        value = message["params"]["arguments"]["value"]
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{
                    "type": "text",
                    "text": (
                        value
                        + ":token=" + os.environ.get("TOKEN", "missing")
                        + ":provider=" + str("PROVIDER_SECRET" in os.environ)
                    ),
                }],
                "isError": False,
            },
        }
    elif method == "notifications/cancelled":
        cancel_path.write_text(json.dumps(message["params"]), encoding="utf-8")
        continue
    else:
        continue
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()
"""


@pytest.mark.asyncio
async def test_stdio_legacy_round_trip_cancellation_and_secret_minimization(
    tmp_path: Path,
) -> None:
    server_path = tmp_path / "fake_mcp_server.py"
    cancellation_path = tmp_path / "cancel.json"
    server_path.write_text(_SERVER, encoding="utf-8")
    spec = McpServerSpec(
        "offline",
        sys.executable,
        tmp_path,
        args=(str(server_path), str(cancellation_path)),
        env_from=(("TOKEN", "MCP_TOKEN"),),
    )
    transport = StdioMcpTransportFactory(
        {
            "PATH": "/usr/bin:/bin",
            "MCP_TOKEN": "resolved-value",
            "PROVIDER_SECRET": "must-not-be-inherited",
        }
    )(spec)
    assert isinstance(transport, StdioMcpTransport)

    info = await transport.connect(timeout_seconds=2.0)
    tools = await transport.list_tools(timeout_seconds=2.0)
    result = await transport.call_tool("echo", {"value": "hello"}, timeout_seconds=2.0)

    assert info.protocol_version == "2025-11-25"
    assert tuple(tool.name for tool in tools) == ("echo",)
    assert result.content == "hello:token=resolved-value:provider=False"

    blocked = asyncio.create_task(
        transport.call_tool("block", {}, timeout_seconds=10.0)
    )
    await asyncio.sleep(0)
    assert transport._pending  # 直接检查协议取消发生在请求仍待响应时。
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked

    # A cancelled request does not poison response routing for later calls.
    after_cancel = await transport.call_tool(
        "echo", {"value": "again"}, timeout_seconds=2.0
    )
    assert after_cancel.content.startswith("again:")
    cancellation = json.loads(cancellation_path.read_text(encoding="utf-8"))
    assert isinstance(cancellation["requestId"], int)
    assert cancellation["reason"] == "MCP request was cancelled"

    await transport.close()
    await transport.close()
    assert not any(
        not task.done() and task.get_name().startswith("my-code:mcp:offline")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )
