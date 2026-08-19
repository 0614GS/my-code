import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nano_code.agent import (
    AgentEngine,
    AgentMaxStepsReached,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentReasoningStarted,
    AgentTextCompleted,
    AgentTextDelta,
    AgentTextStarted,
    AgentTodoListUpdated,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from nano_code.context.attachments.models import (
    ContextAttachment,
    ContextObservation,
)
from nano_code.context.compaction import CompactionCoordinator, CompactionService
from nano_code.context.planner import ContextPlanner
from nano_code.context.window import ContextWindow
from nano_code.conversation import (
    AssistantMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultsMessage,
)
from nano_code.model import (
    ModelContextOverflow,
    ModelOutput,
    ModelOutputCompleted,
    ModelReasoningBlock,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelStreamSequencer,
    ModelTextBlock,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
    ModelToolUseBlock,
    PromptStability,
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
    completed_output_payloads,
)
from nano_code.permissions import PermissionMode, PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.prompts import (
    PromptRegistry,
    PromptSection,
)
from nano_code.sessions import Session, SessionStore
from nano_code.tools import ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from nano_code.tools.result_store import ToolResultStore
from nano_code.tools.round_executor import ToolRoundExecutor
from nano_code.workspace import Workspace


class FakeModel:
    def __init__(self, outputs: list[ModelOutput]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelOutput:
        self.requests.append(request)
        return self.outputs.pop(0)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        output = await self.complete(request)
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(output):
            yield sequencer.emit(payload)


class OverflowOnceModel(FakeModel):
    def __init__(self, outputs: list[ModelOutput]) -> None:
        super().__init__(outputs)
        self.overflowed = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        if not self.overflowed:
            self.overflowed = True
            self.requests.append(request)
            raise ModelContextOverflow("too long")
        async for event in super().stream(request):
            yield event


class NativeLifecycleModel(FakeModel):
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        output = await self.complete(request)
        sequencer = ModelStreamSequencer()
        payloads = (
            ModelReasoningStarted("summary"),
            ModelReasoningDelta("summary", 0, "partial"),
            ModelReasoningCompleted(
                ReasoningPresentation("summary", ("final summary",))
            ),
            ModelTextStarted(),
            ModelTextDelta("dra"),
            ModelTextCompleted("draft corrected"),
            ModelOutputCompleted(output),
        )
        for payload in payloads:
            yield sequencer.emit(payload)


def _engine(
    tmp_path: Path,
    outputs: list[ModelOutput],
    *,
    max_steps: int | None = None,
    model_type: type[FakeModel] = FakeModel,
) -> tuple[AgentEngine, FakeModel, Session, ToolRoundExecutor]:
    store = SessionStore(tmp_path / "sessions", "11111111-1111-1111-1111-111111111111")
    registry = ToolRegistry(builtin_tools())
    executor = ToolExecutor(
        registry,
        PermissionPolicy(PermissionMode.BYPASS),
        HeadlessPrompter(),
        Workspace(tmp_path),
        ToolResultStore(tmp_path / "results"),
    )
    model = model_type(outputs)
    context = ContextPlanner(
        window=ContextWindow(10_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        tools=registry.definitions,
        max_output_tokens=100,
    )
    session = Session(store)
    tool_round = ToolRoundExecutor(executor)
    engine = AgentEngine(
        model_call=model,
        tool_round=tool_round,
        session=session,
        context=context,
        compactor=CompactionCoordinator(context, CompactionService(model)),
        max_steps=max_steps,
    )
    return engine, model, session, tool_round


@pytest.mark.asyncio
async def test_native_stream_lifecycle_is_not_replayed_from_final_output(
    tmp_path: Path,
) -> None:
    reasoning = ModelReasoningBlock(
        "local-reasoning",
        ReasoningPresentation("summary", ("final summary",)),
    )
    engine, _, conversation, _ = _engine(
        tmp_path,
        [ModelOutput((reasoning, ModelTextBlock("draft corrected")), "end_turn")],
        model_type=NativeLifecycleModel,
    )

    events = [event async for event in engine.stream(AgentTurnInput("hello"))]

    assert sum(isinstance(event, AgentReasoningStarted) for event in events) == 1
    assert sum(isinstance(event, AgentReasoningDelta) for event in events) == 1
    assert sum(isinstance(event, AgentReasoningCompleted) for event in events) == 1
    assert sum(isinstance(event, AgentTextStarted) for event in events) == 1
    assert sum(isinstance(event, AgentTextDelta) for event in events) == 1
    assert sum(isinstance(event, AgentTextCompleted) for event in events) == 1
    assistant = conversation.history[-1]
    assert isinstance(assistant, AssistantMessage)
    assert sum(isinstance(block, ReasoningContent) for block in assistant.content) == 1


@pytest.mark.asyncio
async def test_engine_persists_human_and_assistant_messages(tmp_path: Path) -> None:
    engine, model, conversation, _ = _engine(
        tmp_path,
        [ModelOutput((ModelTextBlock("done"),), "end_turn", TokenUsage(3, 1))],
    )
    result = await engine.submit(AgentTurnInput("hello"))

    assert isinstance(result, AgentTurnSucceeded)
    assert result.text == "done"
    assert isinstance(conversation.working_messages[0], HumanMessage)
    assert isinstance(conversation.working_messages[1], AssistantMessage)
    assert model.requests[0].messages[0].content[0] == ModelTextBlock("hello")


@pytest.mark.asyncio
async def test_event_attachment_is_anchored_before_first_call_and_survives_turns(
    tmp_path: Path,
) -> None:
    engine, model, conversation, _ = _engine(
        tmp_path,
        [
            ModelOutput((ModelTextBlock("first"),), "end_turn", TokenUsage(3, 1)),
            ModelOutput((ModelTextBlock("second"),), "end_turn", TokenUsage(4, 1)),
        ],
    )
    attachment = ContextAttachment(
        "file",
        (ContextObservation("File: notes.txt", "     1\thello"),),
        retention="live_session",
    )

    await engine.submit(AgentTurnInput("inspect", (attachment,)))
    await engine.submit(AgentTurnInput("continue"))

    for request in model.requests:
        assert any(
            isinstance(block, ModelTextBlock) and "notes.txt" in block.text
            for message in request.messages
            for block in message.content
        )
    delivery = engine._context_snapshot.attachment_deliveries[0]
    assert delivery.anchor_uuid == conversation.history[0].uuid
    assert conversation.store.load().history == conversation.history


def test_agent_turn_input_rejects_request_only_attachments() -> None:
    with pytest.raises(ValueError, match="live_session"):
        AgentTurnInput(
            "inspect",
            (ContextAttachment("request", (TextContent("temporary"),)),),
        )


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

    result = await engine.submit(AgentTurnInput("read"))

    assert isinstance(result, AgentTurnSucceeded)
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
async def test_engine_hides_thinking_and_replays_it_during_tool_loop(
    tmp_path: Path,
) -> None:
    opaque = ModelReasoningBlock(
        "thinking",
        ReasoningPresentation("verbatim", ("hidden",)),
        ProviderContinuationState(
            ProviderBinding("anthropic-messages", "anthropic", "claude-test"),
            "active_trajectory",
            {"type": "thinking", "thinking": "hidden", "signature": "signed"},
        ),
    )
    engine, model, conversation, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (opaque, ModelToolUseBlock("read", "Read", {"path": "missing"})),
                "tool_use",
                TokenUsage(3, 2),
            ),
            ModelOutput((ModelTextBlock("finished"),), "end_turn", TokenUsage(5, 1)),
        ],
    )

    result = await engine.submit(AgentTurnInput("read"))

    assert isinstance(result, AgentTurnSucceeded)
    assert result.text == "finished"
    assert any(
        isinstance(block, ModelReasoningBlock)
        for message in model.requests[1].messages
        for block in message.content
    )
    persisted = conversation.store.load().history[1]
    assert isinstance(persisted, AssistantMessage)
    assert persisted.content[0].kind == "reasoning"


@pytest.mark.asyncio
async def test_engine_has_no_default_step_limit(tmp_path: Path) -> None:
    outputs = [
        ModelOutput(
            (ModelToolUseBlock(f"read-{step}", "Read", {"path": "missing.txt"}),),
            "tool_use",
            TokenUsage(1, 1),
        )
        for step in range(13)
    ]
    outputs.append(
        ModelOutput((ModelTextBlock("finished"),), "end_turn", TokenUsage(1, 1))
    )
    engine, model, _, _ = _engine(tmp_path, outputs)

    result = await engine.submit(AgentTurnInput("keep going"))

    assert isinstance(result, AgentTurnSucceeded)
    assert result.completed_steps == 14
    assert len(model.requests) == 14


@pytest.mark.asyncio
async def test_explicit_max_steps_returns_structured_terminal_outcome(
    tmp_path: Path,
) -> None:
    engine, model, conversation, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelToolUseBlock("read-1", "Read", {"path": "missing.txt"}),),
                "tool_use",
                TokenUsage(2, 1),
            ),
            ModelOutput(
                (ModelToolUseBlock("read-2", "Read", {"path": "missing.txt"}),),
                "tool_use",
                TokenUsage(3, 1),
            ),
            ModelOutput(
                (ModelTextBlock("continued"),),
                "end_turn",
                TokenUsage(4, 1),
            ),
        ],
        max_steps=2,
    )

    result = await engine.submit(AgentTurnInput("stop at the limit"))

    assert result == AgentMaxStepsReached(
        max_steps=2,
        completed_steps=2,
        usage=TokenUsage(5, 2),
    )
    assert len(model.requests) == 2
    assert isinstance(conversation.history[-1], ToolResultsMessage)

    continued = await engine.submit(AgentTurnInput("continue"))

    assert isinstance(continued, AgentTurnSucceeded)
    assert continued.text == "continued"
    assert len(model.requests) == 3


@pytest.mark.asyncio
async def test_reactive_retry_does_not_increment_completed_steps(
    tmp_path: Path,
) -> None:
    engine, model, _, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (
                    ModelTextBlock(
                        "<analyze>covered</analyze><summary>summary</summary>"
                    ),
                ),
                "end_turn",
                TokenUsage(2, 1),
            ),
            ModelOutput((ModelTextBlock("done"),), "end_turn", TokenUsage(3, 1)),
        ],
        model_type=OverflowOnceModel,
    )

    result = await engine.submit(AgentTurnInput("recover"))

    assert isinstance(result, AgentTurnSucceeded)
    assert result.completed_steps == 1
    assert len(model.requests) == 3


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
    async for event in engine.stream(AgentTurnInput("test it")):
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
        async for event in engine.stream(AgentTurnInput("test it"))
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
        async for event in engine.stream(AgentTurnInput("test it")):
            events.append(event)

    updates = [event for event in events if isinstance(event, AgentTodoListUpdated)]
    assert len(updates) == 1
    assert updates[0].todos[0].content == "Run tests"
    assert isinstance(conversation.history[-1], ToolResultsMessage)
