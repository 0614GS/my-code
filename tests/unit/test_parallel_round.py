"""PAR-01..03 coverage for input-aware ToolRound scheduling."""

import asyncio
from pathlib import Path

import pytest

from my_code.conversation.models import AssistantMessage, ToolCall, ToolResult
from my_code.foundation.json import JsonObject
from my_code.model.primitives import TokenUsage
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionPrompt,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.base import Tool, ToolContext, ToolOutput
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolCallFinished, ToolRoundExecutor
from my_code.workspace.local import Workspace


class BarrierTool(Tool):
    def __init__(self) -> None:
        self.log: list[str] = []
        self._first_entered = asyncio.Event()
        self._second_finished = asyncio.Event()
        self.active = 0
        self.peak = 0

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "Coordinate",
            "Coordinate deterministic scheduler tests.",
            {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "safe": {"type": "boolean"},
                },
                "required": ["label", "safe"],
                "additionalProperties": False,
            },
        )

    def is_concurrency_safe(self, tool_input: JsonObject) -> bool:
        return tool_input.get("safe") is True

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del context
        return self.is_concurrency_safe(tool_input)

    def validate_input(self, tool_input: JsonObject) -> None:
        if not isinstance(tool_input.get("label"), str):
            raise ValueError("label is required")
        if not isinstance(tool_input.get("safe"), bool):
            raise ValueError("safe is required")

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Allowed by scheduler test.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "parallel-test"
            ),
        )

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        del context
        label = tool_input["label"]
        assert isinstance(label, str)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.log.append(f"start:{label}")
        if label == "a":
            self._first_entered.set()
            await self._second_finished.wait()
        elif label == "b":
            await self._first_entered.wait()
            self._second_finished.set()
        elif label == "c":
            assert "end:a" in self.log and "end:b" in self.log
        elif label == "d":
            assert "end:c" in self.log
        self.log.append(f"end:{label}")
        self.active -= 1
        return ToolOutput(label)


class LimitedTool(BarrierTool):
    def __init__(self) -> None:
        super().__init__()
        self.entered: list[str] = []
        self.two_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        del context
        label = tool_input["label"]
        assert isinstance(label, str)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered.append(label)
        if len(self.entered) == 2:
            self.two_entered.set()
        await self.release.wait()
        self.active -= 1
        return ToolOutput(label)


class AskingTool(BarrierTool):
    def __init__(self) -> None:
        super().__init__()
        self.permission_checks = 0
        self.permissions_ready = asyncio.Event()

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del tool_input, context
        self.permission_checks += 1
        if self.permission_checks == 2:
            self.permissions_ready.set()
        return ToolPermissionResult.ask(
            message="Confirm scheduler test.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "parallel-prompt-test"
            ),
        )

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        del context
        label = tool_input["label"]
        assert isinstance(label, str)
        return ToolOutput(label)


class BlockingApprover:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.first_started = asyncio.Event()
        self.release = asyncio.Event()

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        del request
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.first_started.set()
        await self.release.wait()
        self.active -= 1
        return PermissionConfirmation(True)


def build_round(
    tmp_path: Path,
    tool: Tool,
    *,
    max_parallel_calls: int,
    prompter: BlockingApprover | None = None,
) -> ToolRoundExecutor:
    snapshot = ToolCatalogSnapshot.from_tools((tool,))
    return ToolRoundExecutor(
        ToolExecutor(
            tools=snapshot,
            policy=PermissionPolicy(PermissionMode.DEFAULT),
            prompter=prompter or HeadlessPrompter(),
            workspace=Workspace(tmp_path),
        ),
        max_parallel_calls=max_parallel_calls,
    )


def call(call_id: str, label: str, *, safe: bool = True) -> ToolCall:
    return ToolCall(call_id, "Coordinate", {"label": label, "safe": safe})


async def run(
    runner: ToolRoundExecutor,
    calls: tuple[ToolCall, ...],
) -> list[object]:
    assistant = AssistantMessage(calls, TokenUsage())
    return [event async for event in runner.run_round(calls, assistant)]


@pytest.mark.asyncio
async def test_safe_groups_overlap_unsafe_calls_are_barriers_and_order_is_stable(
    tmp_path: Path,
) -> None:
    tool = BarrierTool()
    calls = (
        call("1", "a"),
        call("2", "b"),
        call("3", "c", safe=False),
        call("4", "d"),
    )

    events = await asyncio.wait_for(
        run(build_round(tmp_path, tool, max_parallel_calls=2), calls),
        timeout=1,
    )

    assert tool.peak == 2
    assert set(tool.log[:2]) == {"start:a", "start:b"}
    assert tool.log.index("end:b") < tool.log.index("end:a")
    assert tool.log.index("start:c") > tool.log.index("end:a")
    assert tool.log.index("start:c") > tool.log.index("end:b")
    assert tool.log.index("start:d") > tool.log.index("end:c")
    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    assert [event.call.id for event in finished] == ["1", "2", "3", "4"]
    assert [event.result.tool_use_id for event in finished] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_parallel_limit_is_never_exceeded(tmp_path: Path) -> None:
    tool = LimitedTool()
    calls = tuple(call(str(index), str(index)) for index in range(3))
    runner = build_round(tmp_path, tool, max_parallel_calls=2)
    task = asyncio.create_task(run(runner, calls))

    await asyncio.wait_for(tool.two_entered.wait(), timeout=1)
    assert tool.entered == ["0", "1"]
    assert tool.peak == 2

    tool.release.set()
    await asyncio.wait_for(task, timeout=1)
    assert tool.entered == ["0", "1", "2"]
    assert tool.peak == 2


@pytest.mark.asyncio
async def test_parallel_permission_prompts_are_serialized(tmp_path: Path) -> None:
    tool = AskingTool()
    prompter = BlockingApprover()
    calls = (call("1", "a"), call("2", "b"))
    runner = build_round(
        tmp_path,
        tool,
        max_parallel_calls=2,
        prompter=prompter,
    )
    task = asyncio.create_task(run(runner, calls))

    await asyncio.wait_for(tool.permissions_ready.wait(), timeout=1)
    await asyncio.wait_for(prompter.first_started.wait(), timeout=1)
    assert prompter.calls == 1
    assert prompter.peak == 1

    prompter.release.set()
    events = await asyncio.wait_for(task, timeout=1)
    assert prompter.calls == 2
    assert prompter.peak == 1
    assert (
        len(
            [
                event
                for event in events
                if isinstance(event, ToolCallFinished)
                and isinstance(event.result, ToolResult)
            ]
        )
        == 2
    )
