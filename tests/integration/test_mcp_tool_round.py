"""MCP-02: remote tools use the standard validation/permission/cancel pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from my_code.conversation.models import ToolCall
from my_code.foundation.json import JsonObject
from my_code.mcp.models import McpCallResult, McpRemoteTool
from my_code.mcp.runtime import McpRuntimeError
from my_code.mcp.tool import McpTool
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionMode,
    PermissionPrompt,
    PermissionRule,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.executor import ToolExecutor
from my_code.workspace.local import Workspace


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, JsonObject]] = []
        self.started = asyncio.Event()
        self.block = False
        self.failure: Exception | None = None

    async def call_tool(
        self, server_name: str, remote_name: str, arguments: JsonObject
    ) -> McpCallResult:
        self.calls.append((server_name, remote_name, arguments))
        self.started.set()
        if self.failure is not None:
            raise self.failure
        if self.block:
            await asyncio.Future[None]()
        return McpCallResult(f"remote:{arguments}")


class ApprovingPrompter:
    def __init__(self) -> None:
        self.calls = 0

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        del request
        self.calls += 1
        return PermissionConfirmation(True)


def build_tool(caller: FakeCaller) -> McpTool:
    return McpTool(
        caller,
        server_name="search",
        remote=McpRemoteTool(
            "lookup",
            "Look up a value",
            {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    )


def build_executor(
    tmp_path: Path,
    tool: McpTool,
    *,
    mode: PermissionMode = PermissionMode.DEFAULT,
    rules: tuple[PermissionRule, ...] = (),
    prompter: object | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        tools=ToolCatalogSnapshot.from_tools((tool,)),
        policy=PermissionPolicy(mode, rules),
        prompter=prompter or HeadlessPrompter(),  # type: ignore[arg-type]
        workspace=Workspace(tmp_path),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behavior", "expected_error", "expected_calls"),
    [
        (PermissionBehavior.ALLOW, False, 1),
        (PermissionBehavior.DENY, True, 0),
    ],
)
async def test_explicit_allow_and_deny_rules_wrap_mcp_execution(
    tmp_path: Path,
    behavior: PermissionBehavior,
    expected_error: bool,
    expected_calls: int,
) -> None:
    caller = FakeCaller()
    tool = build_tool(caller)
    executor = build_executor(
        tmp_path,
        tool,
        rules=(PermissionRule(tool.definition.name, behavior),),
    )

    outcome = await executor.execute(
        ToolCall("call-1", tool.definition.name, {"query": "value"})
    )

    assert outcome.result.is_error is expected_error
    assert len(caller.calls) == expected_calls


@pytest.mark.asyncio
async def test_default_mcp_permission_requires_confirmation(tmp_path: Path) -> None:
    caller = FakeCaller()
    tool = build_tool(caller)
    prompter = ApprovingPrompter()
    executor = build_executor(tmp_path, tool, prompter=prompter)

    outcome = await executor.execute(
        ToolCall("call-1", tool.definition.name, {"query": "value"})
    )

    assert outcome.result.is_error is False
    assert outcome.result.content == "remote:{'query': 'value'}"
    assert prompter.calls == 1
    assert caller.calls == [("search", "lookup", {"query": "value"})]


@pytest.mark.asyncio
async def test_invalid_schema_input_stops_before_permission_or_remote_call(
    tmp_path: Path,
) -> None:
    caller = FakeCaller()
    tool = build_tool(caller)
    prompter = ApprovingPrompter()
    executor = build_executor(tmp_path, tool, prompter=prompter)

    outcome = await executor.execute(
        ToolCall("call-1", tool.definition.name, {"query": 42})
    )

    assert outcome.result.is_error is True
    assert "Invalid input" in outcome.result.content
    assert prompter.calls == 0
    assert caller.calls == []


@pytest.mark.asyncio
async def test_connection_error_is_normalized_without_remote_detail(
    tmp_path: Path,
) -> None:
    caller = FakeCaller()
    caller.failure = McpRuntimeError(
        "MCP server 'search' could not complete the tool call"
    )
    tool = build_tool(caller)
    executor = build_executor(tmp_path, tool, mode=PermissionMode.BYPASS)

    outcome = await executor.execute(
        ToolCall("call-1", tool.definition.name, {"query": "secret-detail"})
    )

    assert outcome.result.is_error is True
    assert outcome.result.content == (
        "McpRuntimeError: MCP server 'search' could not complete the tool call"
    )


@pytest.mark.asyncio
async def test_cancelling_mcp_tool_wait_propagates_to_transport_caller(
    tmp_path: Path,
) -> None:
    caller = FakeCaller()
    caller.block = True
    tool = build_tool(caller)
    executor = build_executor(tmp_path, tool, mode=PermissionMode.BYPASS)
    execution = asyncio.create_task(
        executor.execute(ToolCall("call-1", tool.definition.name, {"query": "value"}))
    )
    await caller.started.wait()

    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
