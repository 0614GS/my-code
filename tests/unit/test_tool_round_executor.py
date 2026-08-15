import asyncio
from pathlib import Path

import pytest

from nano_code.agent import (
    ToolCallFinished,
    ToolRoundCompleted,
    ToolRoundEvent,
)
from nano_code.messages import ChatMessage, TextBlock, ToolResultBlock, ToolUseBlock
from nano_code.permissions import PermissionMode, PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from nano_code.tools.result_store import ToolResultStore
from nano_code.tools.round_executor import ToolRoundExecutor


def build_round_executor(tmp_path: Path) -> ToolRoundExecutor:
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=HeadlessPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / "results"),
    )
    return ToolRoundExecutor(executor)


@pytest.mark.asyncio
async def test_round_executor_is_serial_and_returns_one_completed_message(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    runner = build_round_executor(tmp_path)
    assistant = ChatMessage(
        role="assistant",
        origin="model",
        content=(
            ToolUseBlock("first", "Read", {"path": "a.txt"}),
            ToolUseBlock("second", "Read", {"path": "b.txt"}),
        ),
        usage=None,
    )
    calls = tuple(
        block for block in assistant.content if isinstance(block, ToolUseBlock)
    )

    events = [event async for event in runner.run_round(calls, assistant)]

    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    completed = [event for event in events if isinstance(event, ToolRoundCompleted)]
    assert [event.result.tool_use_id for event in finished] == ["first", "second"]
    assert len(completed) == 1
    result_blocks = [
        block
        for block in completed[0].message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert [block.tool_use_id for block in result_blocks] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_round_executor_cancellation_closes_every_call(tmp_path: Path) -> None:
    runner = build_round_executor(tmp_path)
    calls = (
        ToolUseBlock("first", "Read", {"path": "a.txt"}),
        ToolUseBlock("second", "Read", {"path": "b.txt"}),
    )
    assistant = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("working"), *calls),
    )

    async def cancel(_call: ToolUseBlock) -> ToolExecutionOutcome:
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
