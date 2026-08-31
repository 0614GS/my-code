import asyncio
from pathlib import Path

import pytest

from my_code.conversation.models import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResult,
)
from my_code.features.subagents.models import SubagentParentContext
from my_code.features.subagents.tool import SubagentTool
from my_code.model.primitives import TokenUsage
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.builtin import builtin_tools
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from my_code.tools.round_executor import (
    ToolCallFinished,
    ToolRoundCompleted,
    ToolRoundEvent,
    ToolRoundExecutor,
)
from my_code.workspace.local import Workspace


def build_round_executor(tmp_path: Path) -> ToolRoundExecutor:
    executor = ToolExecutor(
        tools=ToolCatalogSnapshot.from_tools(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )
    return ToolRoundExecutor(executor)


@pytest.mark.asyncio
async def test_round_executor_is_serial_and_returns_one_completed_message(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    runner = build_round_executor(tmp_path)
    assistant = AssistantMessage(
        content=(
            ToolCall("first", "Read", {"path": "a.txt"}),
            ToolCall("second", "Read", {"path": "b.txt"}),
        ),
        usage=TokenUsage(),
    )
    calls = tuple(block for block in assistant.content if isinstance(block, ToolCall))

    events = [event async for event in runner.run_round(calls, assistant)]

    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    completed = [event for event in events if isinstance(event, ToolRoundCompleted)]
    assert [event.result.tool_use_id for event in finished] == ["first", "second"]
    assert len(completed) == 1
    result_blocks = [
        block for block in completed[0].message.content if isinstance(block, ToolResult)
    ]
    assert [block.tool_use_id for block in result_blocks] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_round_executor_cancellation_closes_every_call(tmp_path: Path) -> None:
    runner = build_round_executor(tmp_path)
    calls = (
        ToolCall("first", "Read", {"path": "a.txt"}),
        ToolCall("second", "Read", {"path": "b.txt"}),
    )
    assistant = AssistantMessage(
        content=(TextContent("working"), *calls),
        usage=TokenUsage(),
    )

    async def cancel(
        _call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | None = None,
        permission_policy: PermissionPolicy | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> ToolExecutionOutcome:
        del tools, permission_policy, run_id, session_id, root_session_id
        raise asyncio.CancelledError

    runner.executor.execute = cancel  # type: ignore[assignment]
    events: list[ToolRoundEvent] = []
    with pytest.raises(asyncio.CancelledError):
        async for event in runner.run_round(calls, assistant):
            events.append(event)

    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    assert [event.result.tool_use_id for event in finished] == ["first", "second"]
    assert all(event.result.is_error for event in finished)
    assert any(
        isinstance(event, ToolRoundCompleted) and event.cancelled for event in events
    )


def test_subagent_user_abort_projection_is_stable_json(tmp_path: Path) -> None:
    from typing import cast

    from my_code.features.subagents.controller import SubagentController

    runner = build_round_executor(tmp_path)
    tool = SubagentTool(
        cast(SubagentController, object()),
        parent=SubagentParentContext("11111111-1111-1111-1111-111111111111"),
        policy=PermissionPolicy(PermissionMode.BYPASS),
        allow_background=False,
    )
    catalog = ToolCatalogSnapshot.from_tools((tool,))
    call = ToolCall(
        "sub",
        "Subagent",
        {"agent_type": "explore", "description": "inspect", "prompt": "work"},
    )

    result = runner.executor.cancelled_result(call, tools=catalog)

    import json

    assert json.loads(result.content) == {
        "status": "cancelled",
        "reason": "user_abort",
        "agent_type": "explore",
        "message": "Subagent was aborted by the user.",
    }
    assert result.is_error
    assert result.presentation.summary == "Subagent aborted by user"
