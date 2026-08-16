import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nano_code.agent import (
    AgentEngine,
    AgentTodoListUpdated,
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
    ToolCall,
    ToolResultsMessage,
)
from nano_code.permissions import PermissionMode, PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.prompts import PromptRegistry, PromptSection, PromptStability
from nano_code.sessions import SessionStore
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutionOutcome, ToolExecutor
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
) -> tuple[AgentEngine, FakeModel, ConversationState, ToolRoundExecutor]:
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
    conversation = ConversationState(store)
    tool_round = ToolRoundExecutor(executor)
    engine = AgentEngine(
        model_turn=model,
        tool_round=tool_round,
        conversation=conversation,
        context=context,
        compactor=CompactionCoordinator(context, CompactionService(model)),
    )
    return engine, model, conversation, tool_round


@pytest.mark.asyncio
async def test_engine_persists_human_and_assistant_messages(tmp_path: Path) -> None:
    engine, model, conversation, _ = _engine(
        tmp_path,
        [ModelOutput((ModelTextBlock("done"),), "end_turn", TokenUsage(3, 1))],
    )
    result = await engine.submit("hello")

    assert result.text == "done"
    assert isinstance(conversation.working_messages[0], HumanMessage)
    assert isinstance(conversation.working_messages[1], AssistantMessage)
    assert model.requests[0].messages[0].content[0] == ModelTextBlock("hello")


@pytest.mark.asyncio
async def test_engine_closes_tool_loop_and_preserves_results(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    engine, model, conversation, _ = _engine(
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
        for message in conversation.working_messages
        if isinstance(message, ToolResultsMessage)
    ]
    assert len(tool_messages) == 1
    assert "hello" in tool_messages[0].content[0].content
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_engine_emits_todos_only_after_tool_results_are_committed(
    tmp_path: Path,
) -> None:
    engine, _, conversation, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (
                    ModelToolUseBlock(
                        "todo-1",
                        "TodoWrite",
                        {
                            "todos": [
                                {
                                    "content": "Run tests",
                                    "status": "in_progress",
                                    "activeForm": "Running tests",
                                }
                            ]
                        },
                    ),
                ),
                "tool_use",
                TokenUsage(3, 1),
            ),
            ModelOutput((ModelTextBlock("done"),), "end_turn", TokenUsage(3, 1)),
        ],
    )

    updates: list[AgentTodoListUpdated] = []
    async for event in engine.stream("test it"):
        if isinstance(event, AgentTodoListUpdated):
            assert isinstance(conversation.history[-1], ToolResultsMessage)
            updates.append(event)

    assert len(updates) == 1
    assert updates[0].todos[0].content == "Run tests"
    assert engine.status().todos == updates[0].todos


@pytest.mark.asyncio
async def test_failed_todo_write_does_not_emit_todo_update(tmp_path: Path) -> None:
    engine, _, _, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelToolUseBlock("todo-1", "TodoWrite", {"todos": "bad"}),),
                "tool_use",
                TokenUsage(3, 1),
            ),
            ModelOutput((ModelTextBlock("done"),), "end_turn", TokenUsage(3, 1)),
        ],
    )

    updates = [
        event
        async for event in engine.stream("test it")
        if isinstance(event, AgentTodoListUpdated)
    ]

    assert updates == []
    assert engine.status().todos == ()


@pytest.mark.asyncio
async def test_cancelled_round_publishes_committed_todo_before_cancellation(
    tmp_path: Path,
) -> None:
    engine, _, conversation, tool_round = _engine(
        tmp_path,
        [
            ModelOutput(
                (
                    ModelToolUseBlock(
                        "todo-1",
                        "TodoWrite",
                        {
                            "todos": [
                                {
                                    "content": "Run tests",
                                    "status": "in_progress",
                                    "activeForm": "Running tests",
                                }
                            ]
                        },
                    ),
                    ModelToolUseBlock("read-1", "Read", {"path": "missing.txt"}),
                ),
                "tool_use",
                TokenUsage(3, 1),
            ),
        ],
    )
    execute = tool_round.executor.execute

    async def cancel_second(call: ToolCall) -> ToolExecutionOutcome:
        if call.id == "read-1":
            raise asyncio.CancelledError
        return await execute(call)

    tool_round.executor.execute = cancel_second  # type: ignore[assignment]
    events = []
    with pytest.raises(asyncio.CancelledError):
        async for event in engine.stream("test it"):
            events.append(event)

    updates = [event for event in events if isinstance(event, AgentTodoListUpdated)]
    assert len(updates) == 1
    assert updates[0].todos[0].content == "Run tests"
    assert isinstance(conversation.history[-1], ToolResultsMessage)
