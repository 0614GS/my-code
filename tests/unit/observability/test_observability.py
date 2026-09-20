"""Runtime instrumentation and OpenTelemetry mapping."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from my_code.agent.events import AgentEvent, AgentTextStarted
from my_code.agent.models import (
    AgentInvocationSucceeded,
    AgentMaxStepsReached,
    AgentTurnInput,
)
from my_code.context.session_cache import SessionContextCache
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
from my_code.observability.api import (
    EvaluationContext,
    NoOpSpan,
    NoOpTracer,
    OperationTracer,
    RunObservationContext,
    bind_run_context,
)
from my_code.observability.dispatcher import ObservationDispatcher, ObservationSink
from my_code.observability.events import (
    EventOutcome,
    ModelRequestFinished,
    ModelRequestStarted,
    ModelResponseReceived,
)
from my_code.observability.otel import OpenTelemetryBackend
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
        yield AgentInvocationSucceeded(
            "answer", 1, TokenUsage(2, 3, provider_reported=True)
        )

    async def stream_continuation(self, session, runtime) -> AsyncIterator[AgentEvent]:
        yield AgentInvocationSucceeded(
            "continued", 1, TokenUsage(1, 1, provider_reported=True)
        )


class _FailingRunner(_SuccessfulRunner):
    async def stream(self, session, runtime, turn_input):
        del session, runtime, turn_input
        if False:
            yield AgentInvocationSucceeded("", 0, TokenUsage())
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
        yield AgentInvocationSucceeded("answer", 1, TokenUsage())


class _ModelClient:
    def __init__(self, output: ModelOutput) -> None:
        self.output = output

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelStreamEvent(0, ModelOutputCompleted(self.output))


class _BrokenTracer:
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
        OpenTelemetryBackend(
            provider, MeterProvider(), capture_content=capture_content
        ),
        exporter,
    )


def _observations(
    observer: OperationTracer,
    *sinks: ObservationSink,
    capture_content: bool = False,
) -> ObservationDispatcher:
    subscribers: tuple[ObservationSink, ...] = sinks
    if isinstance(observer, OpenTelemetryBackend):
        subscribers = (observer, *subscribers)
    return ObservationDispatcher(observer, subscribers, capture_content=capture_content)


@pytest.mark.asyncio
async def test_agent_adapter_writes_finish_before_terminal_event(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_SuccessfulRunner(), _observations(observer))

    stream = cast(
        AsyncGenerator[AgentEvent, None],
        runner.stream(session, SessionContextCache(), AgentTurnInput("hello")),
    )
    event = await anext(stream)

    assert isinstance(event, AgentInvocationSucceeded)
    assert len(session.invocation_history) == 1
    assert session.invocation_history[0].finished is not None
    assert session.invocation_history[0].finished.outcome == "succeeded"


@pytest.mark.asyncio
async def test_agent_adapter_persists_evaluation_context(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(
        _SuccessfulRunner(),
        _observations(observer),
        evaluation=EvaluationContext("job-1", "case-1", "2"),
    )

    await runner.submit(session, SessionContextCache(), AgentTurnInput("hello"))

    started = session.invocation_history[0].started
    assert (
        started.evaluation_run_id,
        started.test_case_id,
        started.attempt_id,
    ) == ("job-1", "case-1", "2")


@pytest.mark.asyncio
async def test_agent_adapter_records_error_type_without_text(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_FailingRunner(), _observations(observer))

    with pytest.raises(LookupError, match="private failure text"):
        await anext(
            runner.stream(session, SessionContextCache(), AgentTurnInput("hello"))
        )

    finished = session.invocation_history[0].finished
    assert finished is not None
    assert finished.outcome == "failed"
    assert finished.error_type == "LookupError"
    # 直接检查私有存储，确保敏感失败信息没有绕过公开投影写入磁盘。
    assert "private failure text" not in session._store.path.read_text()


@pytest.mark.asyncio
async def test_agent_adapter_records_max_steps_and_continuation(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    max_runner = InstrumentedAgentRunner(_MaxStepsRunner(), _observations(observer))

    await max_runner.submit(session, SessionContextCache(), AgentTurnInput("hello"))
    continuation = InstrumentedAgentRunner(_SuccessfulRunner(), _observations(observer))
    events = [
        event
        async for event in continuation.stream_continuation(
            session, SessionContextCache()
        )
    ]

    assert isinstance(events[-1], AgentInvocationSucceeded)
    assert session.invocation_history[0].finished is not None
    assert session.invocation_history[0].finished.outcome == "max_steps"
    assert session.invocation_history[1].started.continuation is True


@pytest.mark.asyncio
async def test_agent_adapter_records_cancellation(tmp_path) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_CancelledRunner(), _observations(observer))

    with pytest.raises(asyncio.CancelledError):
        await anext(
            runner.stream(session, SessionContextCache(), AgentTurnInput("hello"))
        )

    assert session.invocation_history[0].finished is not None
    assert session.invocation_history[0].finished.outcome == "cancelled"


@pytest.mark.asyncio
async def test_journal_failure_does_not_change_agent_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer, _ = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_StreamingRunner(), _observations(observer))

    def fail(_record) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session, "append_invocation_finished", fail)
    outcome = await runner.submit(
        session, SessionContextCache(), AgentTurnInput("hello")
    )

    assert isinstance(outcome, AgentInvocationSucceeded)


@pytest.mark.asyncio
async def test_agent_adapter_early_close_finishes_span_and_journal(tmp_path) -> None:
    observer, exporter = _observer()
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(_StreamingRunner(), _observations(observer))
    stream = cast(
        AsyncGenerator[AgentEvent, None],
        runner.stream(session, SessionContextCache(), AgentTurnInput("hello")),
    )

    await anext(stream)
    await stream.aclose()

    assert session.invocation_history[0].finished is not None
    assert session.invocation_history[0].finished.outcome == "cancelled"
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
        _ModelClient(output),
        _observations(observer),
        lambda: binding,
        purpose="compaction",
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
        _ModelClient(output),
        _observations(observer, capture_content=True),
        lambda: binding,
        purpose="agent",
    )
    request = ModelRequest(SystemPrompt.from_text("secret prompt"), (), (), 10)

    assert [event async for event in client.stream(request)]

    span = exporter.get_finished_spans()[0]
    response_event = next(
        event for event in span.events if event.name == "model.response.received"
    )
    assert response_event.attributes is not None
    assert response_event.attributes["my_code.event.content.truncated"] is True
    content = response_event.attributes["my_code.event.content"]
    assert isinstance(content, str)
    assert len(content) <= 16 * 1024


@pytest.mark.asyncio
async def test_tool_telemetry_failure_does_not_block_execution() -> None:
    executor = InstrumentedToolExecutor(
        cast(ToolExecutor, _FakeToolExecutor()),
        _observations(cast(OperationTracer, _BrokenTracer())),
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
    observations = _observations(observer)
    prompter = InstrumentedPermissionPrompter(_ApprovingPrompter(), observations)
    audit = TelemetryToolInvocationAudit(observations)
    confirmation: PermissionConfirmation | None = None

    with observations.operation("execute_tool Read"):
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
        event for event in parent.events if event.name == "tool.permission.evaluated"
    )
    assert permission_event.attributes is not None
    assert permission_event.attributes["my_code.event.behavior"] == "ask"
    assert all("feedback" not in str(event.attributes) for event in parent.events)


def test_otel_spans_keep_agent_model_hierarchy() -> None:
    observer, exporter = _observer()
    context = RunObservationContext("session", "run", "turn", "main")

    observations = _observations(observer)
    with bind_run_context(context):
        with observations.operation("invoke_agent main"):
            with observations.operation(
                "chat model", attributes={"gen_ai.operation.name": "chat"}
            ):
                observations.publish(
                    ModelRequestStarted("agent", "provider", "model", step=1)
                )

    child, parent = exporter.get_finished_spans()
    assert child.parent is not None
    assert parent.context is not None
    assert child.parent.span_id == parent.context.span_id
    assert child.attributes is not None
    assert child.attributes["gen_ai.operation.name"] == "chat"


def test_otel_event_sink_correlates_logs_and_records_token_metrics() -> None:
    trace_exporter = InMemorySpanExporter()
    trace_provider = TracerProvider(shutdown_on_exit=False)
    trace_provider.add_span_processor(SimpleSpanProcessor(trace_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(
        metric_readers=(metric_reader,), shutdown_on_exit=False
    )
    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider(shutdown_on_exit=False)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    observer = OpenTelemetryBackend(
        trace_provider,
        meter_provider,
        capture_content=False,
        logger_provider=logger_provider,
    )
    observations = _observations(observer)

    with observations.operation("chat model"):
        observations.publish(
            ModelResponseReceived(
                "agent", "provider", "model", 2, 3, 4, 5, True, "end_turn"
            )
        )
        observations.publish(
            ModelRequestFinished("agent", "provider", "model", EventOutcome.OK, 10)
        )

    logs = log_exporter.get_finished_logs()
    assert len(logs) == 2
    assert logs[0].log_record.trace_id != 0
    assert logs[0].log_record.span_id != 0
    assert logs[0].log_record.attributes is not None
    assert logs[0].log_record.attributes["my_code.event.id"]
    metrics = metric_reader.get_metrics_data()
    assert metrics is not None
    names = {
        metric.name
        for resource in metrics.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert "gen_ai.client.token.usage" in names
    assert "gen_ai.client.operation.duration" in names


@pytest.mark.parametrize("capture_content", [False, True])
@pytest.mark.parametrize(
    "error_type", [ValueError, asyncio.CancelledError, GeneratorExit]
)
def test_span_exception_never_exports_message_or_stacktrace(
    capture_content, error_type
) -> None:
    observer, exporter = _observer(capture_content=capture_content)
    error = error_type("PRIVATE_EXCEPTION_MARKER")
    with pytest.raises(error_type) as caught:
        with observer.start_span("test"):
            raise error
    assert caught.value is error
    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes["error.type"] == error_type.__name__
    assert span.attributes["my_code.outcome"] == (
        "error" if error_type is ValueError else "cancelled"
    )
    assert "PRIVATE_EXCEPTION_MARKER" not in str(span.attributes)
    assert "PRIVATE_EXCEPTION_MARKER" not in str(span.status.description)
    assert not any(event.name == "exception" for event in span.events)


class _BrokenSpan(NoOpSpan):
    def set_attributes(self, attributes):
        raise RuntimeError("span attributes failed")

    def add_event(self, name, attributes=None):
        raise RuntimeError("span event failed")

    def finish(self, outcome=None):
        raise RuntimeError("span finish failed")

    def __exit__(self, exc_type, exc, traceback) -> bool:
        raise RuntimeError("span cleanup failed")


class _BrokenSpanTracer(NoOpTracer):
    def start_span(self, name, **kwargs):
        return _BrokenSpan()


@pytest.mark.asyncio
async def test_all_span_write_failures_preserve_model_output() -> None:
    output = ModelOutput(
        (ModelTextBlock("answer"),), "end_turn", TokenUsage(1, 2, 3, 4, True)
    )
    client = InstrumentedModelClient(
        _ModelClient(output),
        _observations(_BrokenSpanTracer()),
        lambda: ProviderBinding("test", "provider", "model"),
        purpose="agent",
    )
    events = [
        event
        async for event in client.stream(
            ModelRequest(SystemPrompt.from_text("prompt"), (), (), 10)
        )
    ]
    assert events[-1].payload == ModelOutputCompleted(output)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner_type, error_type",
    [(_FailingRunner, LookupError), (_CancelledRunner, asyncio.CancelledError)],
)
async def test_span_write_failures_preserve_agent_failure_and_cancel(
    tmp_path, runner_type, error_type
) -> None:
    session = Session(tmp_path, SESSION_ID)
    runner = InstrumentedAgentRunner(runner_type(), _observations(_BrokenSpanTracer()))
    with pytest.raises(error_type):
        await runner.submit(session, SessionContextCache(), AgentTurnInput("hello"))
    assert session.invocation_history[0].finished is not None
    assert session.invocation_history[0].finished.outcome == (
        "cancelled" if error_type is asyncio.CancelledError else "failed"
    )


def test_duration_failure_does_not_leave_active_span(monkeypatch) -> None:
    from opentelemetry import trace

    observer, exporter = _observer()
    previous = trace.get_current_span()

    def fail(*args):
        raise RuntimeError("metrics failed")

    monkeypatch.setattr(observer, "record_duration", fail)
    with pytest.raises(RuntimeError, match="metrics failed"):
        with observer.start_span("test"):
            pass
    assert trace.get_current_span() is previous
    assert len(exporter.get_finished_spans()) == 1


def test_shutdown_budget_is_shared_and_does_not_wait_for_stuck_provider(
    monkeypatch,
) -> None:
    from threading import Event

    observer, _ = _observer()
    entered = Event()
    release = Event()
    finished = Event()

    def stuck():
        entered.set()
        release.wait()
        finished.set()

    monkeypatch.setattr(observer._tracer_provider, "shutdown", stuck)
    try:
        observer.shutdown(timeout_millis=0)
        assert entered.wait(1)
        assert not finished.is_set()
    finally:
        release.set()
        observer.shutdown(timeout_millis=1000)
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LookupError("private"), asyncio.CancelledError()])
async def test_tool_error_and_cancel_survive_broken_span(tmp_path, error) -> None:
    import json

    from my_code.observability.diagnostic_log import JsonlObservationSink

    class FailingExecutor(_FakeToolExecutor):
        async def execute(self, call, **kwargs):
            raise error

    diagnostics = JsonlObservationSink(tmp_path / "diagnostic.jsonl")
    observations = _observations(_BrokenSpanTracer(), diagnostics)
    executor = InstrumentedToolExecutor(
        cast(ToolExecutor, FailingExecutor()),
        observations,
    )
    try:
        with pytest.raises(type(error)) as caught:
            await executor.execute(ToolCall("call", "Read", {"path": "private"}))
        assert caught.value is error
    finally:
        diagnostics.close()
    records = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    assert records[-1]["event"] == "tool.execution.finished"
    assert records[-1]["outcome"] == (
        "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
    )
    assert records[-1]["tool_call_id"] == "call"
    assert "private" not in diagnostics.path.read_text()
