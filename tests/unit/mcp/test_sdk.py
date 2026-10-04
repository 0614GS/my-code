"""验证官方 MCP SDK 到本地 transport 的协议和结果转换。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from mcp.client.auth import OAuthClientProvider

import my_code.mcp.sdk as sdk_module
from my_code.auth.mcp_bearer import McpBearerTokenStore
from my_code.mcp.models import (
    McpAuthChallenge,
    McpAuthKind,
    McpServerSpec,
    McpServerTransport,
)
from my_code.mcp.sdk import SdkMcpTransportFactory
from my_code.mcp.transport import (
    McpAuthenticationRequired,
    McpConfigurationError,
    McpRequestError,
)

_SERVER = r"""
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "server/discover":
        response = {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"}}
    elif method == "initialize":
        response = {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
            "serverInfo": {"name": "sdk-test", "version": "1"}}}
    elif method == "tools/list":
        response = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{
            "name": "echo", "description": "Echo", "inputSchema": {
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"]}}]}}
    elif method == "tools/call":
        if message["params"]["name"] == "block":
            continue
        value = message["params"]["arguments"]["value"]
        response = {"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": value}],
            "isError": False}}
    else:
        continue
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
"""


@pytest.mark.asyncio
async def test_sdk_stdio_round_trip_timeout_and_close(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(_SERVER, encoding="utf-8")
    spec = McpServerSpec("remote", sys.executable, tmp_path, args=(str(server),))
    transport = SdkMcpTransportFactory(
        {"PATH": "/usr/bin:/bin"}, tmp_path, interactive=False
    )(spec)

    try:
        info = await transport.connect(timeout_seconds=3)
        assert info.server_name == "sdk-test"
        assert info.protocol_version == "2025-11-25"
        assert [
            tool.name for tool in await transport.list_tools(timeout_seconds=3)
        ] == ["echo"]
        assert (
            await transport.call_tool("echo", {"value": "hello"}, timeout_seconds=3)
        ).content == "hello"
        with pytest.raises(McpRequestError, match="timeout"):
            await transport.call_tool("block", {}, timeout_seconds=0.05)
        assert (
            await transport.call_tool("echo", {"value": "again"}, timeout_seconds=3)
        ).content == "again"
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_sdk_stdio_connect_timeout_cancels_actor(tmp_path: Path) -> None:
    server = tmp_path / "silent.py"
    server.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    spec = McpServerSpec("silent", sys.executable, tmp_path, args=(str(server),))
    transport = SdkMcpTransportFactory({}, tmp_path, interactive=False)(spec)

    with pytest.raises(TimeoutError):
        await transport.connect(timeout_seconds=0.05)
    assert transport._actor is None


def test_sdk_bearer_resolves_only_named_environment_variable(tmp_path: Path) -> None:
    spec = McpServerSpec(
        "remote",
        None,
        tmp_path,
        transport=McpServerTransport.HTTP,
        url="https://example.com/mcp",
        auth=McpAuthKind.BEARER,
        bearer_token_from="MCP_TOKEN",
    )
    factory = SdkMcpTransportFactory(
        {"MCP_TOKEN": "secret", "PROVIDER_SECRET": "other"},
        tmp_path,
        interactive=False,
    )
    transport = factory(spec)
    assert transport._bearer_token == "secret"
    assert "PROVIDER_SECRET" not in transport._environment
    with pytest.raises(McpConfigurationError, match="Bearer token source"):
        SdkMcpTransportFactory({}, tmp_path, interactive=False)(spec)


def test_sdk_bearer_can_load_user_private_token(tmp_path: Path) -> None:
    url = "https://example.com/mcp"
    McpBearerTokenStore(tmp_path / ".mcp-bearer", "remote", url).save("stored-secret")
    spec = McpServerSpec(
        "remote",
        None,
        tmp_path,
        transport=McpServerTransport.HTTP,
        url=url,
        auth=McpAuthKind.BEARER,
    )
    transport = SdkMcpTransportFactory({}, tmp_path / ".mcp-oauth", interactive=False)(
        spec
    )
    assert transport._bearer_token == "stored-secret"


@pytest.mark.asyncio
async def test_sdk_tool_call_cancellation_does_not_poison_connection(
    tmp_path: Path,
) -> None:
    server = tmp_path / "server.py"
    server.write_text(_SERVER, encoding="utf-8")
    spec = McpServerSpec("remote", sys.executable, tmp_path, args=(str(server),))
    transport = SdkMcpTransportFactory({}, tmp_path, interactive=False)(spec)
    try:
        await transport.connect(timeout_seconds=3)
        blocked = asyncio.create_task(
            transport.call_tool("block", {}, timeout_seconds=10)
        )
        await asyncio.sleep(0.05)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert (
            await transport.call_tool("echo", {"value": "ok"}, timeout_seconds=3)
        ).content == "ok"
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth", [McpAuthKind.AUTO, McpAuthKind.BEARER, McpAuthKind.OAUTH]
)
async def test_sdk_http_auth_is_passed_to_sdk_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: McpAuthKind
) -> None:
    captured: dict[str, Any] = {}

    class FakeHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

    class FakeClient:
        protocol_version = "2025-11-25"
        server_info = None
        server_capabilities = SimpleNamespace(tools=None)

        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def list_tools(self, *, cursor: str | None = None) -> Any:
            return SimpleNamespace(tools=[], next_cursor=None)

    class FakeCallback:
        redirect_uri = "http://127.0.0.1:1234/callback"

        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> FakeCallback:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def redirect(self, _: str) -> None:
            pass

        async def callback(self) -> Any:
            raise AssertionError("No authorization request expected")

    monkeypatch.setattr(sdk_module.httpx2, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(sdk_module, "Client", FakeClient)
    monkeypatch.setattr(sdk_module, "LoopbackOAuthCallback", FakeCallback)
    monkeypatch.setattr(
        sdk_module, "streamable_http_client", lambda *_args, **_kwargs: object()
    )
    spec = McpServerSpec(
        "remote",
        None,
        tmp_path,
        transport=McpServerTransport.HTTP,
        url="https://example.com/mcp",
        auth=auth,
        bearer_token_from="MCP_TOKEN" if auth is McpAuthKind.BEARER else None,
    )
    transport = SdkMcpTransportFactory(
        {"MCP_TOKEN": "secret"}, tmp_path, interactive=True
    )(spec)
    try:
        assert (await transport.connect(timeout_seconds=1)).server_name == "remote"
        assert await transport.list_tools(timeout_seconds=1) == ()
    finally:
        await transport.close()

    if auth is McpAuthKind.BEARER:
        assert captured["headers"] == {"Authorization": "Bearer secret"}
        assert captured["auth"] is None
    elif auth is McpAuthKind.OAUTH:
        assert captured["headers"] == {}
        assert isinstance(captured["auth"], OAuthClientProvider)
    else:
        assert captured["headers"] == {}
        assert captured["auth"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("challenge_header", "metadata", "expected"),
    [
        ('Bearer realm="test"', None, McpAuthChallenge.BEARER),
        (
            'Bearer resource_metadata="https://example.com/meta"',
            {
                "resource": "https://example.com/mcp",
                "authorization_servers": ["https://login.example.com"],
            },
            McpAuthChallenge.OAUTH,
        ),
        (None, None, McpAuthChallenge.CHOOSE),
    ],
)
async def test_auto_http_auth_classifies_401_without_real_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    challenge_header: str | None,
    metadata: dict[str, object] | None,
    expected: McpAuthChallenge,
) -> None:
    original_client = httpx2.AsyncClient
    requested: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        if request.url.path == "/mcp":
            return httpx2.Response(
                401,
                headers=(
                    {"WWW-Authenticate": challenge_header}
                    if challenge_header is not None
                    else {}
                ),
                request=request,
            )
        if request.url.path == "/meta" and metadata is not None:
            return httpx2.Response(200, json=metadata, request=request)
        return httpx2.Response(404, request=request)

    monkeypatch.setattr(
        sdk_module.httpx2,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx2.MockTransport(respond), **kwargs
        ),
    )
    spec = McpServerSpec(
        "remote",
        None,
        tmp_path,
        transport=McpServerTransport.HTTP,
        url="https://example.com/mcp",
        auth=McpAuthKind.AUTO,
    )
    transport = SdkMcpTransportFactory({}, tmp_path, interactive=False)(spec)

    with pytest.raises(McpAuthenticationRequired) as failure:
        await transport.connect(timeout_seconds=2)
    assert failure.value.challenge is expected
    assert all(url.startswith("https://example.com/") for url in requested)
