import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nano_code.agent import (
    AgentEngine,
    AgentHistoryUserMessage,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
    ContextPlan,
    ConversationState,
    ModelResponseCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ToolInteractionPort,
    ToolRoundCompleted,
    ToolRoundEvent,
)
from nano_code.agent.errors import ModelContextOverflow
from nano_code.context import CompactionCoordinator, ContextPlanner, ContextWindow
from nano_code.context.compaction import CompactionService
from nano_code.messages import (
    ChatMessage,
    ModelResponse,
    SystemContextBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.permissions import PermissionMode, PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.prompts import PromptRegistry, PromptSection, PromptStability
from nano_code.sessions import SessionStore
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.interaction import ToolRoundExecutor
from nano_code.tools.result_store import ToolResultStore

_SESSION_ID = "12345678-1234-1234-1234-123456789abc"
_OTHER_SESSION_ID = "87654321-4321-4321-4321-cba987654321"


class FakeProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ContextPlan] = []

    async def complete(self, request: ContextPlan) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def stream(self, request: ContextPlan) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        response = self.responses.pop(0)
        for block in response.content:
            if isinstance(block, TextBlock):
                midpoint = max(1, len(block.text) // 2)
                yield ModelTextDelta(block.text[:midpoint])
                yield ModelTextDelta(block.text[midpoint:])
        yield ModelResponseCompleted(response)


class ReactiveOverflowProvider(FakeProvider):
    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(responses)
        self._overflow_pending = True

    async def stream(self, request: ContextPlan) -> AsyncIterator[ModelStreamEvent]:
        if self._overflow_pending:
            self._overflow_pending = False
            self.requests.append(request)
            raise ModelContextOverflow("test overflow")
        async for event in super().stream(request):
            yield event


class MissingFinalProvider(FakeProvider):
    async def stream(self, request: ContextPlan) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        yield ModelTextDelta("partial response")


def build_engine(
    tmp_path: Path,
    provider: FakeProvider,
    *,
    context_chars: int = 160_000,
    tool_interaction: ToolInteractionPort | None = None,
) -> AgentEngine:
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    registry = ToolRegistry(builtin_tools())
    executor = ToolExecutor(
        registry=registry,
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=HeadlessPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(store.session_dir / "tool-results"),
    )
    context = ContextPlanner(
        window=ContextWindow(context_chars),
        prompt=PromptRegistry(
            (PromptSection("test", PromptStability.STATIC, lambda: "test"),)
        ),
        tools=registry.definitions,
        max_output_tokens=8192,
    )
    return AgentEngine(
        model_turn=provider,
        tool_interaction=tool_interaction or ToolRoundExecutor(executor),
        conversation=ConversationState(store),
        context=context,
        compactor=CompactionCoordinator(context, CompactionService(provider)),
    )


class CancellingInteraction:
    """A port fake that exercises the engine's cancellation safety net."""

    def bind_session(self, session_id: str) -> None:
        del session_id

    def present_use(self, call: ToolUseBlock) -> ToolUsePresentation:
        return ToolUsePresentation(call.name, call.name, f"Running {call.name}")

    def present_stored_result(
        self, call: ToolUseBlock, result: ToolResultBlock | None
    ) -> ToolResultPresentation:
        del call
        return ToolResultPresentation(summary=result.content if result else "cancelled")

    async def run_round(
        self,
        calls: tuple[ToolUseBlock, ...],
        assistant_message: ChatMessage,
    ) -> AsyncIterator[ToolRoundEvent]:
        if False:
            yield ToolRoundCompleted(assistant_message, ())  # pragma: no cover
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_overflow_compacts_to_persisted_boundary_and_releases_working_set(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    old_user = ChatMessage(role="user", origin="human", content=(TextBlock("x" * 200),))
    old_answer = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("old answer"),),
        parent_uuid=old_user.uuid,
    )
    store.append(old_user)
    store.append(old_answer)
    provider = FakeProvider(
        [
            ModelResponse(
                content=(TextBlock("short continuation summary"),),
                stop_reason="end_turn",
            ),
            ModelResponse(content=(TextBlock("continued"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider, context_chars=150)

    result = await engine.submit("new")

    assert result.text == "continued"
    assert len(provider.requests) == 2
    assert provider.requests[0].tools == ()
    assert "compact coding-agent" in provider.requests[0].system_prompt.text
    active_store = SessionStore(tmp_path / "state", _SESSION_ID)
    boundaries = active_store.load_compact_boundaries()
    assert len(boundaries) == 1
    assert boundaries[0].trigger == "auto"
    full_history = active_store.load()
    working_set = active_store.load_working_set(full_history)
    assert len(full_history) == 5
    assert len(working_set) == 2
    assert working_set[0].origin == "system"
    summary_block = working_set[0].content[0]
    assert isinstance(summary_block, SystemContextBlock)
    assert summary_block.kind == "conversation_summary"
    assert "short continuation summary" in summary_block.content
    assert engine.state().message_count == len(working_set)

    resumed_engine = build_engine(tmp_path, FakeProvider([]), context_chars=150)
    assert resumed_engine.state().message_count == len(working_set)


@pytest.mark.asyncio
async def test_provider_overflow_triggers_one_reactive_compact(tmp_path: Path) -> None:
    provider = ReactiveOverflowProvider(
        [
            ModelResponse(
                content=(TextBlock("reactive summary"),), stop_reason="end_turn"
            ),
            ModelResponse(content=(TextBlock("recovered"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider)

    result = await engine.submit("hello")

    assert result.text == "recovered"
    assert len(provider.requests) == 3
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    assert store.load_compact_boundaries()[0].trigger == "reactive"


@pytest.mark.asyncio
async def test_missing_final_model_event_is_rejected(tmp_path: Path) -> None:
    provider = MissingFinalProvider([])
    engine = build_engine(tmp_path, provider)

    with pytest.raises(RuntimeError, match="without a final response"):
        await engine.submit("hello")

    assert engine.message_count == 1
    assert engine.working_messages[0].content[0] == TextBlock("hello")


@pytest.mark.asyncio
async def test_failed_compaction_does_not_write_boundary_or_release_messages(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    original = ChatMessage(role="user", origin="human", content=(TextBlock("keep me"),))
    store.append(original)
    provider = FakeProvider(
        [ModelResponse(content=(TextBlock("   "),), stop_reason="end_turn")]
    )
    engine = build_engine(tmp_path, provider)

    with pytest.raises(RuntimeError, match="no text summary"):
        await engine.compact()

    assert engine.state().message_count == 1
    assert store.load_compact_boundaries() == ()
    assert store.load() == (original,)


@pytest.mark.asyncio
async def test_runs_tool_round_and_persists_protocol_pair(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    provider = FakeProvider(
        [
            ModelResponse(
                content=(
                    ToolUseBlock(
                        id="tool-1",
                        name="Read",
                        input={"path": "hello.txt"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("done"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider)

    result = await engine.submit("read it")

    assert result.text == "done"
    assert result.turns == 2
    assert len(provider.requests) == 2
    second_request = provider.requests[1]
    tool_result = second_request.messages[-1].content[0]
    assert tool_result.type == "tool_result"
    assert tool_result.tool_use_id == "tool-1"
    assert "hello" in tool_result.content
    assert tool_result.presentation is None
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    assert len(store.load()) == 4


@pytest.mark.asyncio
async def test_stream_exposes_text_and_tool_lifecycle_in_order(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    provider = FakeProvider(
        [
            ModelResponse(
                content=(ToolUseBlock("tool-1", "Read", {"path": "hello.txt"}),),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("done"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider)

    events = [event async for event in engine.stream("read it")]

    assert [type(event) for event in events] == [
        AgentToolStarted,
        AgentToolFinished,
        AgentTextDelta,
        AgentTextDelta,
        AgentTurnCompleted,
    ]
    assert (
        "".join(event.text for event in events if isinstance(event, AgentTextDelta))
        == "done"
    )
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    assert len(store.load()) == 4


@pytest.mark.asyncio
async def test_headless_write_ask_is_returned_as_denied_result(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                content=(
                    ToolUseBlock(
                        id="tool-write",
                        name="Write",
                        input={"path": "created.txt", "content": "unsafe"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("denied"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider)

    result = await engine.submit("write it")

    assert result.text == "denied"
    assert not (tmp_path / "created.txt").exists()
    denied = provider.requests[1].messages[-1].content[0]
    assert denied.type == "tool_result"
    assert denied.tool_use_id == "tool-write"
    assert denied.is_error is True
    assert "approval was not provided" in denied.content


@pytest.mark.asyncio
async def test_cancellation_persists_results_for_every_tool_use(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                content=(
                    ToolUseBlock("first", "Read", {"path": "a.txt"}),
                    ToolUseBlock("second", "Read", {"path": "b.txt"}),
                ),
                stop_reason="tool_use",
            )
        ]
    )
    engine = build_engine(
        tmp_path,
        provider,
        tool_interaction=CancellingInteraction(),
    )

    with pytest.raises(asyncio.CancelledError):
        await engine.submit("read both")

    store = SessionStore(tmp_path / "state", _SESSION_ID)
    persisted = store.load()
    assert all(isinstance(result, ToolResultBlock) for result in persisted[-1].content)
    tool_results = [
        result
        for result in persisted[-1].content
        if isinstance(result, ToolResultBlock)
    ]
    assert [result.tool_use_id for result in tool_results] == ["first", "second"]
    assert all(result.type == "tool_result" for result in tool_results)
    assert all(result.is_error for result in tool_results)


def test_resume_repairs_trailing_unresolved_tool_uses(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    user_message = ChatMessage(
        role="user", origin="human", content=(TextBlock("read"),)
    )
    assistant_message = ChatMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("interrupted", "Read", {"path": "a.txt"}),),
        parent_uuid=user_message.uuid,
    )
    store.append(user_message)
    store.append(assistant_message)

    build_engine(tmp_path, FakeProvider([]))

    persisted = SessionStore(tmp_path / "state", _SESSION_ID).load()
    assert len(persisted) == 3
    repair = persisted[-1].content[0]
    assert isinstance(repair, ToolResultBlock)
    assert repair.tool_use_id == "interrupted"
    assert repair.is_error is True


def test_resume_switches_store_and_messages_after_validating_target(
    tmp_path: Path,
) -> None:
    current_store = SessionStore(tmp_path / "state", _SESSION_ID)
    current = ChatMessage(role="user", origin="human", content=(TextBlock("current"),))
    current_store.append(current)
    engine = build_engine(tmp_path, FakeProvider([]))
    target_store = SessionStore(tmp_path / "state", _OTHER_SESSION_ID)
    target = ChatMessage(role="user", origin="human", content=(TextBlock("target"),))
    target_store.append(target)

    loaded = engine.resume(target_store)

    assert loaded.state.session_id == _OTHER_SESSION_ID
    assert loaded.state.message_count == 1
    assert loaded.history == (AgentHistoryUserMessage("target"),)


def test_failed_resume_preserves_current_engine_state(tmp_path: Path) -> None:
    engine = build_engine(tmp_path, FakeProvider([]))
    current_state = engine.state()
    broken_store = SessionStore(tmp_path / "state", _OTHER_SESSION_ID)
    broken_store.project_state_dir.mkdir(parents=True, exist_ok=True)
    broken_store.path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid transcript line"):
        engine.resume(broken_store)

    assert engine.state() == current_state


def test_resume_rejects_missing_or_empty_session(tmp_path: Path) -> None:
    engine = build_engine(tmp_path, FakeProvider([]))
    empty_store = SessionStore(tmp_path / "state", _OTHER_SESSION_ID)

    with pytest.raises(ValueError, match="contains no messages"):
        engine.resume(empty_store)

    assert engine.state().session_id == _SESSION_ID
