from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nano_code.agent import (
    AgentEngine,
    ConversationState,
    ModelOutput,
    ModelOutputCompleted,
    ModelRequest,
    ModelStreamEvent,
    ModelTextBlock,
    ModelToolUseBlock,
)
from nano_code.context import CompactionCoordinator, ContextPlanner, ContextWindow
from nano_code.context.compaction import CompactionService
from nano_code.messages import (
    AssistantMessage,
    HumanMessage,
    TokenUsage,
    ToolResultsMessage,
)
from nano_code.permissions import PermissionMode, PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.prompts import PromptRegistry, PromptSection, PromptStability
from nano_code.sessions import SessionStore
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.result_store import ToolResultStore
from nano_code.tools.round_executor import ToolRoundExecutor


class FakeModel:
    def __init__(self, outputs: list[ModelOutput]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelOutput:
        self.requests.append(request)
        return self.outputs.pop(0)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        output = await self.complete(request)
        yield ModelOutputCompleted(output)


def _engine(
    tmp_path: Path, outputs: list[ModelOutput]
) -> tuple[AgentEngine, FakeModel]:
    store = SessionStore(tmp_path / "sessions", "11111111-1111-1111-1111-111111111111")
    registry = ToolRegistry(builtin_tools())
    executor = ToolExecutor(
        registry,
        PermissionPolicy(PermissionMode.BYPASS),
        HeadlessPrompter(),
        ToolContext(tmp_path),
        ToolResultStore(tmp_path / "results"),
    )
    model = FakeModel(outputs)
    context = ContextPlanner(
        window=ContextWindow(10_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=registry.definitions,
        max_output_tokens=100,
    )
    engine = AgentEngine(
        model_turn=model,
        tool_round=ToolRoundExecutor(executor),
        conversation=ConversationState(store),
        context=context,
        compactor=CompactionCoordinator(context, CompactionService(model)),
    )
    return engine, model


@pytest.mark.asyncio
async def test_engine_persists_human_and_assistant_messages(tmp_path: Path) -> None:
    engine, model = _engine(
        tmp_path,
        [ModelOutput((ModelTextBlock("done"),), "end_turn", TokenUsage(3, 1))],
    )
    result = await engine.submit("hello")

    assert result.text == "done"
    assert isinstance(engine.working_messages[0], HumanMessage)
    assert isinstance(engine.working_messages[1], AssistantMessage)
    assert model.requests[0].messages[0].content[0] == ModelTextBlock("hello")


@pytest.mark.asyncio
async def test_engine_closes_tool_loop_and_preserves_results(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    engine, model = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelToolUseBlock("read", "Read", {"path": "hello.txt"}),),
                "tool_use",
                TokenUsage(3, 1),
            ),
            ModelOutput((ModelTextBlock("finished"),), "end_turn", TokenUsage(5, 1)),
        ],
    )

    result = await engine.submit("read")

    assert result.text == "finished"
    tool_messages = [
        message
        for message in engine.working_messages
        if isinstance(message, ToolResultsMessage)
    ]
    assert len(tool_messages) == 1
    assert "hello" in tool_messages[0].content[0].content
    assert len(model.requests) == 2
