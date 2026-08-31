import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.events import (
    AgentCompactionCompleted,
    AgentCompactionStarted,
    AgentConversationUpdated,
    AgentInputAccepted,
    AgentModelStepCompleted,
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
    UserTurnInput,
)
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.models import ContextOverflow
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextRuntime
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
from my_code.model.errors import ModelContextOverflow, ModelProtocolError
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
        output = self.outputs.pop(0)
        if not output.usage.provider_reported:
            return replace(output, usage=TokenUsage(1, 1, provider_reported=True))
        return output

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


class QueuedSource:
    def __init__(self, batches: list[tuple[UserTurnInput, ...]]) -> None:
        self.batches = batches
        self.accepted: list[str] = []

    async def drain_pending(self) -> tuple[UserTurnInput, ...]:
        return self.batches.pop(0) if self.batches else ()

    def accept_pending(self, input_ids) -> None:
        self.accepted.extend(input_ids)


class BoundEngine:
    """Test fixture binding explicit per-session resources to a stateless engine."""

    def __init__(
        self,
        engine: AgentEngine,
        session: Session,
    ) -> None:
        self.engine = engine
        self.session = session
        self.runtime = ContextRuntime()

    async def submit(self, turn_input: AgentTurnInput):
        return await self.engine.submit(self.session, self.runtime, turn_input)

    def stream(self, turn_input: AgentTurnInput):
        return self.engine.stream(self.session, self.runtime, turn_input)

    @property
    def history(self):
        return self.session.conversation

    @property
    def context_state(self):
        return self.session.context_planning_state()


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
async def test_request_audit_failure_prevents_provider_delivery(tmp_path: Path) -> None:
    output = ModelOutput((ModelTextBlock("never sent"),), "end_turn")
    bound, model, session, _ = _engine(tmp_path, [output])

    def fail_audit(_invocation: object) -> None:
        raise OSError("audit disk unavailable")

    session.prepare_model_invocation = fail_audit  # type: ignore[method-assign]

    with pytest.raises(OSError, match="audit disk unavailable"):
        await bound.submit(AgentTurnInput("hello"))

    assert model.requests == []


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
    assert [
        (event.step_index, event.has_tools)
        for event in events
        if isinstance(event, AgentModelStepCompleted)
    ] == [(1, False)]
    assistant = conversation.conversation[-1]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.content[-1] == TextContent("draft corrected")
    assert sum(isinstance(block, ReasoningContent) for block in assistant.content) == 1


@pytest.mark.parametrize(
    ("events", "message"),
    (
        (
            (ModelStreamEvent(1, ModelTextStarted()),),
            "non-contiguous sequence",
        ),
        (
            (
                ModelStreamEvent(0, ModelTextStarted()),
                ModelStreamEvent(1, ModelReasoningStarted("summary")),
            ),
            "overlapping display blocks",
        ),
        (
            (
                ModelStreamEvent(0, ModelReasoningStarted("summary")),
                ModelStreamEvent(1, ModelReasoningDelta("verbatim", 0, "detail")),
            ),
            "changed reasoning disclosure",
        ),
        (
            (
                ModelStreamEvent(0, ModelTextStarted()),
                ModelStreamEvent(
                    1,
                    ModelOutputCompleted(
                        ModelOutput((ModelTextBlock("done"),), "end_turn")
                    ),
                ),
            ),
            "ended with an active display block",
        ),
        (
            (
                ModelStreamEvent(0, ModelTextStarted()),
                ModelStreamEvent(1, ModelTextCompleted("done")),
            ),
            "ended without a final response",
        ),
    ),
)
@pytest.mark.asyncio
async def test_engine_rejects_invalid_model_stream_protocol(
    tmp_path: Path,
    events: tuple[ModelStreamEvent, ...],
    message: str,
) -> None:
    engine, model, session, _ = _engine(
        tmp_path,
        [ModelOutput((ModelTextBlock("unused"),), "end_turn")],
    )

    async def invalid_stream(
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        del request
        for event in events:
            yield event

    model.stream = invalid_stream  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match=message):
        _ = [event async for event in engine.stream(AgentTurnInput("hello"))]

    assert len(session.conversation) == 1
    assert isinstance(session.conversation[0], HumanMessage)


@pytest.mark.asyncio
async def test_missing_provider_usage_does_not_commit_assistant_fact(
    tmp_path: Path,
) -> None:
    engine, model, session, _ = _engine(
        tmp_path,
        [ModelOutput((ModelTextBlock("unused"),), "end_turn")],
    )

    async def missing_usage(
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelStreamEvent(
            0,
            ModelOutputCompleted(ModelOutput((ModelTextBlock("answer"),), "end_turn")),
        )

    model.stream = missing_usage  # type: ignore[method-assign]
    with pytest.raises(ModelProtocolError, match="without valid token usage"):
        _ = [event async for event in engine.stream(AgentTurnInput("hello"))]

    assert len(session.conversation) == 1
    assert isinstance(session.conversation[0], HumanMessage)


@pytest.mark.asyncio
async def test_engine_persists_human_and_assistant_messages(tmp_path: Path) -> None:
    engine, model, conversation, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelTextBlock("done"),),
                "end_turn",
                TokenUsage(3, 1, provider_reported=True),
            )
        ],
    )
    result = await engine.submit(AgentTurnInput("hello"))

    assert isinstance(result, AgentTurnSucceeded)
    assert result.text == "done"
    assert isinstance(conversation.context_entries[0], HumanMessage)
    assert isinstance(conversation.context_entries[1], AssistantMessage)
    first_input = model.requests[0].input[0]
    assert first_input.content[0] == InputText("hello")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_continuation_does_not_append_a_human_message(tmp_path: Path) -> None:
    engine, _, session, _ = _engine(
        tmp_path,
        [ModelOutput((ModelTextBlock("continued"),), "end_turn")],
    )
    session.append_human_message(HumanMessage("original prompt"))

    events = [
        event
        async for event in engine.engine.stream_continuation(session, engine.runtime)
    ]

    assert isinstance(events[-1], AgentTurnSucceeded)
    assert sum(isinstance(item, HumanMessage) for item in session.conversation) == 1
    assert isinstance(session.conversation[-1], AssistantMessage)


@pytest.mark.asyncio
async def test_no_tool_step_accepts_fifo_steering_before_ending(tmp_path: Path) -> None:
    engine, model, session, _ = _engine(
        tmp_path,
        [
            ModelOutput((ModelTextBlock("first"),), "end_turn"),
            ModelOutput((ModelTextBlock("second"),), "end_turn"),
        ],
    )
    source = QueuedSource(
        [
            (UserTurnInput("initial", input_id="one"),),
            (UserTurnInput("steer", input_id="two"),),
            (),
        ]
    )

    events = [
        event
        async for event in engine.engine.stream_continuation(
            session, engine.runtime, pending_source=source
        )
    ]

    assert source.accepted == ["one", "two"]
    assert [
        event.prompt for event in events if isinstance(event, AgentInputAccepted)
    ] == ["initial", "steer"]
    assert [type(item) for item in session.conversation] == [
        HumanMessage,
        AssistantMessage,
        HumanMessage,
        AssistantMessage,
    ]
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_steering_never_splits_tool_use_and_result_batch(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    engine, _, session, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelToolUseBlock("read", "Read", {"path": "hello.txt"}),),
                "tool_use",
            ),
            ModelOutput((ModelTextBlock("done"),), "end_turn"),
        ],
    )
    source = QueuedSource(
        [
            (UserTurnInput("initial", input_id="one"),),
            (UserTurnInput("after tool", input_id="two"),),
            (),
        ]
    )

    _ = [
        event
        async for event in engine.engine.stream_continuation(
            session, engine.runtime, pending_source=source
        )
    ]

    assert [type(item) for item in session.conversation] == [
        HumanMessage,
        AssistantMessage,
        ToolResultBatch,
        HumanMessage,
        AssistantMessage,
    ]


@pytest.mark.asyncio
async def test_max_steps_leaves_boundary_input_for_next_invocation(
    tmp_path: Path,
) -> None:
    engine, _, session, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelToolUseBlock("read", "Read", {"path": "missing.txt"}),),
                "tool_use",
            )
        ],
        max_steps=1,
    )
    source = QueuedSource(
        [
            (UserTurnInput("initial", input_id="one"),),
            (UserTurnInput("later", input_id="two"),),
        ]
    )

    events = [
        event
        async for event in engine.engine.stream_continuation(
            session, engine.runtime, pending_source=source
        )
    ]

    assert isinstance(events[-1], AgentMaxStepsReached)
    assert source.accepted == ["one"]
    assert source.batches == [(UserTurnInput("later", input_id="two"),)]


@pytest.mark.asyncio
async def test_event_attachment_is_anchored_before_first_call_and_survives_turns(
    tmp_path: Path,
) -> None:
    engine, model, conversation, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelTextBlock("first"),),
                "end_turn",
                TokenUsage(3, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("second"),),
                "end_turn",
                TokenUsage(4, 1, provider_reported=True),
            ),
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
    delivered = conversation.conversation[1]
    assert isinstance(delivered, AttachmentMessage)
    assert delivered.parent_uuid == conversation.conversation[0].uuid
    assert (
        Session(tmp_path / "sessions", conversation.session_id).conversation
        == conversation.conversation
    )


@pytest.mark.asyncio
async def test_replacement_session_does_not_inherit_attachment(
    tmp_path: Path,
) -> None:
    engine, _, _, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelTextBlock("first"),),
                "end_turn",
                TokenUsage(3, 1, provider_reported=True),
            )
        ],
    )
    attachment = FileMentionAttachment("notes.txt", "hello")
    await engine.submit(AgentTurnInput("inspect", (attachment,)))
    assert any(
        isinstance(message, AttachmentMessage)
        for message in engine.context_state.context_entries
    )

    replacement = Session(
        tmp_path / "sessions",
        "22222222-2222-2222-2222-222222222222",
    )
    engine.session = replacement

    assert not any(
        isinstance(message, AttachmentMessage)
        for message in engine.context_state.context_entries
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
                TokenUsage(3, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("finished"),),
                "end_turn",
                TokenUsage(5, 1, provider_reported=True),
            ),
        ],
    )

    result = await engine.submit(AgentTurnInput("read"))

    assert isinstance(result, AgentTurnSucceeded)
    assert result.text == "finished"
    tool_messages = [
        message
        for message in conversation.context_entries
        if isinstance(message, ToolResultBatch)
    ]
    assert len(tool_messages) == 1
    assert "hello" in tool_messages[0].content[0].content
    assert len(model.requests) == 2
    assert result.completed_steps == 2
    history = conversation.conversation
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
async def test_permission_mode_change_during_step_applies_to_next_step(
    tmp_path: Path,
) -> None:
    catalog = ToolCatalog()
    catalog.register_source(ToolSourceId("test", "permissions"), builtin_tools())
    policy = PermissionPolicy(PermissionMode.DEFAULT)
    executor = ToolExecutor(
        catalog.snapshot(),
        policy,
        HeadlessPrompter(),
        Workspace(tmp_path),
    )
    outputs = [
        ModelOutput(
            (
                ModelToolUseBlock(
                    "first", "Write", {"path": "first.txt", "content": "first"}
                ),
            ),
            "tool_use",
        ),
        ModelOutput(
            (
                ModelToolUseBlock(
                    "second", "Write", {"path": "second.txt", "content": "second"}
                ),
            ),
            "tool_use",
        ),
        ModelOutput((ModelTextBlock("done"),), "end_turn"),
    ]
    model = FakeModel(outputs)
    original_complete = model.complete

    async def change_mode_during_first_step(request: ModelRequest) -> ModelOutput:
        output = await original_complete(request)
        if len(model.requests) == 1:
            policy.mode = PermissionMode.BYPASS
        return output

    model.complete = change_mode_during_first_step  # type: ignore[method-assign]
    planner = ContextPlanner(
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
    )
    session = Session(tmp_path / "sessions", "22222222-2222-2222-2222-222222222222")
    engine = AgentEngine(
        model_call=model,
        tool_round=ToolRoundExecutor(executor),
        context=ContextEngine(planner, ContextCompactor(model)),
        tool_catalog=catalog,
    )

    result = await engine.submit(session, ContextRuntime(), AgentTurnInput("write"))

    assert isinstance(result, AgentTurnSucceeded)
    batches = [
        item for item in session.conversation if isinstance(item, ToolResultBatch)
    ]
    assert batches[0].content[0].is_error
    assert "Permission denied" in batches[0].content[0].content
    assert not (tmp_path / "first.txt").exists()
    assert batches[1].content[0].is_error is False
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second"


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
                TokenUsage(3, 2, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("finished"),),
                "end_turn",
                TokenUsage(5, 1, provider_reported=True),
            ),
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
    restored = Session(tmp_path / "sessions", conversation.session_id)
    persisted = restored.conversation[1]
    assert isinstance(persisted, AssistantMessage)
    assert persisted.content[0].kind == "reasoning"
    assert not hasattr(persisted.content[0], "continuation")
    assert len(restored.context_planning_state().replay_records) == 1


@pytest.mark.asyncio
async def test_engine_has_no_default_step_limit(tmp_path: Path) -> None:
    outputs = [
        ModelOutput(
            (ModelToolUseBlock(f"read-{step}", "Read", {"path": "missing.txt"}),),
            "tool_use",
            TokenUsage(1, 1, provider_reported=True),
        )
        for step in range(13)
    ]
    outputs.append(
        ModelOutput(
            (ModelTextBlock("finished"),),
            "end_turn",
            TokenUsage(1, 1, provider_reported=True),
        )
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
                TokenUsage(2, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelToolUseBlock("read-2", "Read", {"path": "missing.txt"}),),
                "tool_use",
                TokenUsage(3, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("continued"),),
                "end_turn",
                TokenUsage(4, 1, provider_reported=True),
            ),
        ],
        max_steps=2,
    )

    result = await engine.submit(AgentTurnInput("stop at the limit"))

    assert result == AgentMaxStepsReached(
        max_steps=2,
        completed_steps=2,
        usage=TokenUsage(5, 2, provider_reported=True),
    )
    assert len(model.requests) == 2
    assert isinstance(conversation.conversation[-1], ToolResultBatch)

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
                TokenUsage(2, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("done"),),
                "end_turn",
                TokenUsage(3, 1, provider_reported=True),
            ),
        ],
        model_type=OverflowOnceModel,
    )

    events = [event async for event in engine.stream(AgentTurnInput("recover"))]
    result = events[-1]

    assert isinstance(result, AgentTurnSucceeded)
    assert result.completed_steps == 1
    assert len(model.requests) == 3
    lifecycle = [
        event
        for event in events
        if isinstance(event, (AgentCompactionStarted, AgentCompactionCompleted))
    ]
    assert lifecycle == [
        AgentCompactionStarted("reactive"),
        AgentCompactionCompleted("reactive", TokenUsage(2, 1, provider_reported=True)),
    ]


@pytest.mark.asyncio
async def test_proactive_compaction_events_surround_the_durable_commit(
    tmp_path: Path,
) -> None:
    engine, _, session, _ = _engine(
        tmp_path,
        [
            ModelOutput(
                (ModelTextBlock("summary"),),
                "end_turn",
                TokenUsage(4, 2, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("done"),),
                "end_turn",
                TokenUsage(3, 1, provider_reported=True),
            ),
        ],
    )
    context = engine.engine._context
    original_plan = context.plan
    attempts = 0

    def overflow_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ContextOverflow(10_001, 10_000)
        return original_plan(*args, **kwargs)

    context.plan = overflow_once  # type: ignore[method-assign]
    events = [event async for event in engine.stream(AgentTurnInput("compact"))]

    started = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, AgentCompactionStarted)
    )
    completed = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, AgentCompactionCompleted)
    )
    assert events[started] == AgentCompactionStarted("auto")
    assert events[completed] == AgentCompactionCompleted(
        "auto", TokenUsage(4, 2, provider_reported=True)
    )
    assert started < completed
    assert session.compact_count == 1


@pytest.mark.asyncio
async def test_failed_proactive_compaction_has_no_completed_event(
    tmp_path: Path,
) -> None:
    engine, _, session, _ = _engine(tmp_path, [])
    context = engine.engine._context

    def overflow(*args, **kwargs):
        del args, kwargs
        raise ContextOverflow(10_001, 10_000)

    context.plan = overflow  # type: ignore[method-assign]
    context.compact = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("summary failed")
    )
    events = []

    with pytest.raises(RuntimeError, match="summary failed"):
        async for event in engine.stream(AgentTurnInput("compact")):
            events.append(event)

    lifecycle = [
        event
        for event in events
        if isinstance(event, (AgentCompactionStarted, AgentCompactionCompleted))
    ]
    assert lifecycle == [AgentCompactionStarted("auto")]
    assert session.compact_count == 0


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
                TokenUsage(3, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("done"),),
                "end_turn",
                TokenUsage(3, 1, provider_reported=True),
            ),
        ],
    )

    updates: list[AgentConversationUpdated] = []
    async for event in engine.stream(AgentTurnInput("test it")):
        if isinstance(event, AgentConversationUpdated):
            assert isinstance(conversation.conversation[-1], ToolResultBatch)
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
                TokenUsage(3, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("done"),),
                "end_turn",
                TokenUsage(3, 1, provider_reported=True),
            ),
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
                TokenUsage(3, 1, provider_reported=True),
            ),
        ],
    )
    execute = tool_round.executor.execute

    async def cancel_second(
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | None = None,
        permission_policy: PermissionPolicy | None = None,
        run_id: str | None = None,
    ) -> ToolExecutionOutcome:
        if call.id == "read-1":
            raise asyncio.CancelledError
        return await execute(
            call,
            tools=tools,
            permission_policy=permission_policy,
            run_id=run_id,
        )

    tool_round.executor.execute = cancel_second  # type: ignore[assignment]
    events = []
    with pytest.raises(asyncio.CancelledError):
        async for event in engine.stream(AgentTurnInput("test it")):
            events.append(event)

    updates = [event for event in events if isinstance(event, AgentConversationUpdated)]
    assert len(updates) == 1
    assert project_todos(engine.history).todos[0].content == "Run tests"
    assert isinstance(conversation.conversation[-1], ToolResultBatch)
