"""Runtime instrumentation and OpenTelemetry mapping."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from my_code.agent.events import AgentEvent, AgentTextStarted
from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from my_code.context.session import ContextRuntime
from my_code.conversation.models import ToolCall, ToolResult
from my_code.conversation.presentation import generic_tool_result_presentation
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.primitives import ProviderBinding, TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    SystemPrompt,
)
from my_code.observability.api import Observer, RunObservationContext
from my_code.observability.otel import OpenTelemetryObserver
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionPrompt,
)
from my_code.runtime.instrumentation import (
    InstrumentedAgentRunner,
    InstrumentedModelClient,
    InstrumentedPermissionPrompter,
    InstrumentedToolExecutor,
    TelemetryToolInvocationAudit,
)
from my_code.sessions.session import Session
from my_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from my_code.tools.invocation import ToolInvocation

SESSION_ID = "11111111-1111-1111-1111-111111111111"


class _SuccessfulRunner:
    async def submit(self, session, runtime, turn_input):  # pragma: no cover
        raise AssertionError

    async def stream(self, session, runtime, turn_input) -> AsyncIterator[AgentEvent]:
        yield AgentTurnSucceeded("answer", 1, TokenUsage(2, 3, provider_reported=True))

    async def stream_continuation(self, session, runtime) -> AsyncIterator[AgentEvent]:
        yield AgentTurnSucceeded(
            "continued", 1, TokenUsage(1, 1, provider_reported=True)
        )


class _FailingRunner(_SuccessfulRunner):
    async def stream(self, session, runtime, turn_input):
        del session, runtime, turn_input
        if False:
            yield AgentTurnSucceeded("", 0, TokenUsage())
        raise LookupError("private failure text")


class _MaxStepsRunner(_SuccessfulRunner):
    async def stream(self, session, runtime, turn_input):
        del session, runtime, turn_input
        yield AgentMaxStepsReached(2, 2, TokenUsage(4, 5, provider_reported=True))


class _CancelledRunner(_SuccessfulRunner):
    async def stream(self, session, runtime, turn_input):
        del session, runtime, turn_input
        if False:
            yield AgentTextStarted()
        raise asyncio.CancelledError


class _StreamingRunner(_SuccessfulRunner):
    async def stream(self, session, runtime, turn_input):
        del session, runtime, turn_input
        yield AgentTextStarted()
        yield AgentTurnSucceeded("answer", 1, TokenUsage())


class _ModelClient:
    def __init__(self, output: ModelOutput) -> None:
        self.output = output

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelStreamEvent(0, ModelOutputCompleted(self.output))


class _BrokenObserver:
    def bind_run(self, context):
        raise RuntimeError("sink unavailable")

    def start_span(self, name, **kwargs):
        raise RuntimeError("sink unavailable")

    def record(self, event_type, payload):
        raise RuntimeError("sink unavailable")

    def shutdown(self, timeout_millis=2_000):
        pass


class _FakeToolExecutor:
    tools = object()

    async def execute(self, call, **kwargs):
        del kwargs
        return ToolExecutionOutcome(
            ToolResult(
                call.id,
                "ok",
                generic_tool_result_presentation("ok", False),
            )
        )


class _ApprovingPrompter:
    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        del request
        return PermissionConfirmation(True)


def _observer(*, capture_content: bool = False):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return (
        OpenTelemetryObserver(
            provider, MeterProvider(), capture_content=capture_content
        ),
        exporter,
    )


@pytest.mark.asyncio
async def test_agent_adapter_writes_finish_before_terminal_event(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_SuccessfulRunner(), observer)

    stream = cast(
        AsyncGenerator[AgentEvent, None],
        runner.stream(session, ContextRuntime(), AgentTurnInput("hello")),
    )
    event = await anext(stream)

    assert isinstance(event, AgentTurnSucceeded)
    assert len(session.turn_history) == 1
    assert session.turn_history[0].finished is not None
    assert session.turn_history[0].finished.outcome == "succeeded"


@pytest.mark.asyncio
async def test_agent_adapter_records_error_type_without_text(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_FailingRunner(), observer)

    with pytest.raises(LookupError, match="private failure text"):
        await anext(runner.stream(session, ContextRuntime(), AgentTurnInput("hello")))

    finished = session.turn_history[0].finished
    assert finished is not None
    assert finished.outcome == "failed"
    assert finished.error_type == "LookupError"
    assert "private failure text" not in session._store.path.read_text()  # noqa: SLF001


@pytest.mark.asyncio
async def test_agent_adapter_records_max_steps_and_continuation(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    max_runner = InstrumentedAgentRunner(_MaxStepsRunner(), observer)

    await max_runner.submit(session, ContextRuntime(), AgentTurnInput("hello"))
    continuation = InstrumentedAgentRunner(_SuccessfulRunner(), observer)
    events = [
        event
        async for event in continuation.stream_continuation(session, ContextRuntime())
    ]

    assert isinstance(events[-1], AgentTurnSucceeded)
    assert session.turn_history[0].finished is not None
    assert session.turn_history[0].finished.outcome == "max_steps"
    assert session.turn_history[1].started.continuation is True


@pytest.mark.asyncio
async def test_agent_adapter_records_cancellation(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_CancelledRunner(), observer)

    with pytest.raises(asyncio.CancelledError):
        await anext(runner.stream(session, ContextRuntime(), AgentTurnInput("hello")))

    assert session.turn_history[0].finished is not None
    assert session.turn_history[0].finished.outcome == "cancelled"


@pytest.mark.asyncio
async def test_journal_failure_does_not_change_agent_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_StreamingRunner(), observer)

    def fail(_record) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session, "append_turn_finished", fail)
    outcome = await runner.submit(session, ContextRuntime(), AgentTurnInput("hello"))

    assert isinstance(outcome, AgentTurnSucceeded)


@pytest.mark.asyncio
async def test_agent_adapter_early_close_finishes_span_and_journal(tmp_path) -> None:
    observer, exporter = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_StreamingRunner(), observer)
    stream = cast(
        AsyncGenerator[AgentEvent, None],
        runner.stream(session, ContextRuntime(), AgentTurnInput("hello")),
    )

    await anext(stream)
    await stream.aclose()

    assert session.turn_history[0].finished is not None
    assert session.turn_history[0].finished.outcome == "cancelled"
    assert exporter.get_finished_spans()[0].name == "invoke_agent main"


@pytest.mark.asyncio
async def test_model_adapter_sets_metadata_and_omits_content_by_default() -> None:
    observer, exporter = _observer()
    binding = ProviderBinding("test", "provider", "model")
    output = ModelOutput(
        (ModelTextBlock("secret answer"),),
        "end_turn",
        TokenUsage(2, 3, provider_reported=True),
    )
    client = InstrumentedModelClient(
        _ModelClient(output), observer, lambda: binding, purpose="compaction"
    )
    request = ModelRequest(SystemPrompt.from_text("secret prompt"), (), (), 10)

    assert [event async for event in client.stream(request)]

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes["my_code.model.purpose"] == "compaction"
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert all("secret" not in str(event.attributes) for event in span.events)


@pytest.mark.asyncio
async def test_model_content_capture_is_explicit_and_truncated() -> None:
    observer, exporter = _observer(capture_content=True)
    binding = ProviderBinding("test", "provider", "model")
    output = ModelOutput(
        (ModelTextBlock("x" * 20_000),),
        "end_turn",
        TokenUsage(2, 3, provider_reported=True),
    )
    client = InstrumentedModelClient(
        _ModelClient(output), observer, lambda: binding, purpose="agent"
    )
    request = ModelRequest(SystemPrompt.from_text("secret prompt"), (), (), 10)

    assert [event async for event in client.stream(request)]

    span = exporter.get_finished_spans()[0]
    response_event = next(
        event for event in span.events if event.name == "model.response"
    )
    assert response_event.attributes is not None
    assert response_event.attributes["my_code.event.content.truncated"] is True
    content = response_event.attributes["my_code.event.content"]
    assert isinstance(content, str)
    assert len(content) <= 16 * 1024


@pytest.mark.asyncio
async def test_tool_telemetry_failure_does_not_block_execution() -> None:
    executor = InstrumentedToolExecutor(
        cast(ToolExecutor, _FakeToolExecutor()), cast(Observer, _BrokenObserver())
    )

    outcome = await executor.execute(ToolCall("call", "Read", {"path": "x"}))

    assert outcome.result.content == "ok"


@pytest.mark.asyncio
async def test_permission_wait_is_child_span_and_audit_is_structured() -> None:
    observer, exporter = _observer()
    decision = PermissionDecision(
        PermissionBehavior.ASK,
        "confirm",
        PermissionDecisionReason(PermissionDecisionKind.MODE, "default-mode"),
    )
    prompt = PermissionPrompt("Read", {"path": "x"}, decision, "Read", "x", "read")
    prompter = InstrumentedPermissionPrompter(_ApprovingPrompter(), observer)
    audit = TelemetryToolInvocationAudit(observer)
    confirmation: PermissionConfirmation | None = None

    with observer.start_span("execute_tool Read"):
        confirmation = await prompter.confirm(prompt)
        await audit.record_permission(
            ToolInvocation(), ToolCall("call", "Read", {"path": "x"}), decision
        )

    child, parent = exporter.get_finished_spans()
    assert confirmation is not None and confirmation.allowed is True
    assert child.name == "tool.blocked_on_user"
    assert child.parent is not None and parent.context is not None
    assert child.parent.span_id == parent.context.span_id
    permission_event = next(
        event for event in parent.events if event.name == "tool.permission"
    )
    assert permission_event.attributes is not None
    assert permission_event.attributes["my_code.event.behavior"] == "ask"
    assert all("feedback" not in str(event.attributes) for event in parent.events)


def test_otel_spans_keep_agent_model_hierarchy() -> None:
    observer, exporter = _observer()
    context = RunObservationContext("session", "run", "turn", "main")

    with observer.bind_run(context):
        with observer.start_span("invoke_agent main"):
            with observer.start_span(
                "chat model", attributes={"gen_ai.operation.name": "chat"}
            ):
                observer.record("model.request", {"request": "secret", "step": 1})

    child, parent = exporter.get_finished_spans()
    assert child.parent is not None
    assert parent.context is not None
    assert child.parent.span_id == parent.context.span_id
    assert child.attributes is not None
    assert child.attributes["gen_ai.operation.name"] == "chat"
