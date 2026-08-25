import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.events import (
    AgentConversationUpdated,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentReasoningStarted,
    AgentTextCompleted,
    AgentTextDelta,
    AgentTextStarted,
)
from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.window import ContextWindow
from my_code.conversation.attachments import FileMentionAttachment
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultBatch,
)
from my_code.features.todos.projection import project_todos
from my_code.features.todos.tool import TodoWriteTool
from my_code.model.errors import ModelContextOverflow
from my_code.model.events import (
    ModelOutputCompleted,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelStreamEvent,
    ModelStreamSequencer,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
    completed_output_payloads,
)
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
)
from my_code.model.request import (
    AssistantOutput,
    InputText,
    ModelOutput,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    PromptStability,
)
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.sessions.session import Session
from my_code.tools.builtin import builtin_tools
from my_code.tools.catalog import (
    ToolCatalog,
    ToolCatalogSnapshot,
    ToolSourceId,
)
from my_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


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


class BoundEngine:
    """Test fixture binding explicit per-session resources to a stateless engine."""

    def __init__(
        self,
        engine: AgentEngine,
        session: Session,
    ) -> None:
        self.engine = engine
        self.session = session

    async def submit(self, turn_input: AgentTurnInput):
        return await self.engine.submit(self.session, turn_input)

    def stream(self, turn_input: AgentTurnInput):
        return self.engine.stream(self.session, turn_input)

    @property
    def history(self):
        return self.session.snapshot().history

    @property
    def context_snapshot(self):
        return self.session.context_snapshot()


def _engine(
    tmp_path: Path,
    outputs: list[ModelOutput],
    *,
    max_steps: int | None = None,
    model_type: type[FakeModel] = FakeModel,
) -> tuple[BoundEngine, FakeModel, Session, ToolRoundExecutor]:
    session_id = "11111111-1111-1111-1111-111111111111"
    catalog = ToolCatalog()
    catalog.register_source(
        ToolSourceId("test", "agent-engine"),
        (*builtin_tools(), TodoWriteTool()),
    )
    tools = catalog.snapshot()
    executor = ToolExecutor(
        tools,
        PermissionPolicy(PermissionMode.BYPASS),
        HeadlessPrompter(),
        Workspace(tmp_path),
    )
    model = model_type(outputs)
    planner = ContextPlanner(
        window=ContextWindow(10_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
    )
    context = ContextEngine(planner, ContextCompactor(model))
    session = Session(tmp_path / "sessions", session_id)
    tool_round = ToolRoundExecutor(executor)
    engine = AgentEngine(
        model_call=model,
        tool_round=tool_round,
        context=context,
        tool_catalog=catalog,
        max_steps=max_steps,
    )
    return BoundEngine(engine, session), model, session, tool_round


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
    assistant = conversation.snapshot().history[-1]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.content[-1] == TextContent("draft corrected")
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
    assert isinstance(conversation.snapshot().working_set[0], HumanMessage)
    assert isinstance(conversation.snapshot().working_set[1], AssistantMessage)
    first_input = model.requests[0].input[0]
    assert first_input.content[0] == InputText("hello")  # type: ignore[union-attr]


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
    attachment = FileMentionAttachment("notes.txt", "     1\thello")

    await engine.submit(AgentTurnInput("inspect", (attachment,)))
    await engine.submit(AgentTurnInput("continue"))

    for request in model.requests:
        assert any(
            isinstance(block, InputText) and "notes.txt" in block.text
            for item in request.input
            if hasattr(item, "content")
            for block in item.content  # type: ignore[union-attr]
        )
    delivered = conversation.snapshot().history[1]
    assert isinstance(delivered, AttachmentMessage)
    assert delivered.parent_uuid == conversation.snapshot().history[0].uuid
    assert (
        Session(tmp_path / "sessions", conversation.session_id).snapshot().history
        == conversation.snapshot().history
    )


@pytest.mark.asyncio
async def test_replacement_session_does_not_inherit_attachment(
    tmp_path: Path,
) -> None:
    engine, _, _, _ = _engine(
        tmp_path,
        [ModelOutput((ModelTextBlock("first"),), "end_turn", TokenUsage(3, 1))],
    )
    attachment = FileMentionAttachment("notes.txt", "hello")
    await engine.submit(AgentTurnInput("inspect", (attachment,)))
    assert any(
        isinstance(message, AttachmentMessage)
        for message in engine.context_snapshot.messages
    )

    replacement = Session(
        tmp_path / "sessions",
        "22222222-2222-2222-2222-222222222222",
    )
    engine.session = replacement

    assert not any(
        isinstance(message, AttachmentMessage)
        for message in engine.context_snapshot.messages
    )


@pytest.mark.asyncio
async def test_one_human_turn_can_contain_multiple_steps_and_one_tool_round(
    tmp_path: Path,
) -> None:
    """A turn starts at Human input; each model call is a step."""

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
        for message in conversation.snapshot().working_set
        if isinstance(message, ToolResultBatch)
    ]
    assert len(tool_messages) == 1
    assert "hello" in tool_messages[0].content[0].content
    assert len(model.requests) == 2
    assert result.completed_steps == 2
    history = conversation.snapshot().history
    assert [message.kind for message in history] == [
        "human",
        "assistant",
        "tool_result_batch",
        "assistant",
    ]
    first_assistant = history[1]
    tool_round = history[2]
    assert isinstance(first_assistant, AssistantMessage)
    assert isinstance(tool_round, ToolResultBatch)
    assert tool_round.source_assistant_id == first_assistant.uuid


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
        for item in model.requests[1].input
        if isinstance(item, AssistantOutput)
        for block in item.content
    )
    restored = Session(tmp_path / "sessions", conversation.session_id).snapshot()
    persisted = restored.history[1]
    assert isinstance(persisted, AssistantMessage)
    assert persisted.content[0].kind == "reasoning"
    assert not hasattr(persisted.content[0], "continuation")
    assert len(restored.replay_records) == 1


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
    assert isinstance(conversation.snapshot().history[-1], ToolResultBatch)

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
async def test_engine_announces_conversation_only_after_tool_results_are_committed(
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

    updates: list[AgentConversationUpdated] = []
    async for event in engine.stream(AgentTurnInput("test it")):
        if isinstance(event, AgentConversationUpdated):
            assert isinstance(conversation.snapshot().history[-1], ToolResultBatch)
            updates.append(event)

    assert len(updates) == 1
    assert project_todos(engine.history).todos[0].content == "Run tests"


@pytest.mark.asyncio
async def test_failed_todo_write_commits_without_changing_todo_projection(
    tmp_path: Path,
) -> None:
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
        if isinstance(event, AgentConversationUpdated)
    ]

    assert len(updates) == 1
    assert project_todos(engine.history).todos == ()


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

    async def cancel_second(
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | None = None,
        run_id: str | None = None,
    ) -> ToolExecutionOutcome:
        if call.id == "read-1":
            raise asyncio.CancelledError
        return await execute(call, tools=tools, run_id=run_id)

    tool_round.executor.execute = cancel_second  # type: ignore[assignment]
    events = []
    with pytest.raises(asyncio.CancelledError):
        async for event in engine.stream(AgentTurnInput("test it")):
            events.append(event)

    updates = [event for event in events if isinstance(event, AgentConversationUpdated)]
    assert len(updates) == 1
    assert project_todos(engine.history).todos[0].content == "Run tests"
    assert isinstance(conversation.snapshot().history[-1], ToolResultBatch)
