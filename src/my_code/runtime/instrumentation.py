"""Failure-isolated observability adapters at the runtime composition boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TypeVar
from uuid import uuid4

from my_code.agent.events import AgentEvent
from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnOutcome,
    AgentTurnSucceeded,
)
from my_code.agent.runner import AgentRunner
from my_code.context.session import ContextRuntime
from my_code.conversation.models import ToolCall, ToolResult
from my_code.conversation.presentation import ToolResultPresentation
from my_code.model.client import ModelClient
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.primitives import ProviderBinding
from my_code.model.request import ModelRequest
from my_code.observability.api import (
    EvaluationContext,
    NoOpSpan,
    ObservationOutcome,
    ObservationSpan,
    Observer,
    RunObservationContext,
    SpanKind,
)
from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionDecision,
    PermissionMode,
    PermissionPrompt,
    PermissionPrompter,
    PermissionUpdate,
)
from my_code.sessions.models import TurnFinished, TurnStarted
from my_code.sessions.session import Session
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import ToolExposureSnapshot
from my_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from my_code.tools.invocation import ToolInvocation, ToolInvocationAudit
from my_code.tools.presentation import ToolUsePresentation

logger = logging.getLogger(__name__)
_JournalRecord = TypeVar("_JournalRecord", TurnStarted, TurnFinished)


class InstrumentedAgentRunner:
    def __init__(
        self,
        runner: AgentRunner,
        observer: Observer,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        agent_name: str = "main",
        evaluation: EvaluationContext | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = runner
        self._observer = observer
        self._run_id = run_id
        self._parent_run_id = parent_run_id
        self._agent_name = agent_name
        self._evaluation = evaluation
        self._clock = clock or (lambda: datetime.now(UTC))

    async def submit(
        self, session: Session, runtime: ContextRuntime, turn_input: AgentTurnInput
    ) -> AgentTurnOutcome:
        outcome: AgentTurnOutcome | None = None
        async for event in self.stream(session, runtime, turn_input):
            if isinstance(event, (AgentTurnSucceeded, AgentMaxStepsReached)):
                outcome = event
        if outcome is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return outcome

    def stream(
        self, session: Session, runtime: ContextRuntime, turn_input: AgentTurnInput
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(session, runtime, turn_input, continuation=False)

    def stream_continuation(
        self, session: Session, runtime: ContextRuntime
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(session, runtime, None, continuation=True)

    async def _stream(
        self,
        session: Session,
        runtime: ContextRuntime,
        turn_input: AgentTurnInput | None,
        *,
        continuation: bool,
    ) -> AsyncIterator[AgentEvent]:
        turn_id = str(uuid4())
        run_id = self._run_id or session.session_id
        evaluation = self._evaluation
        started = TurnStarted(
            turn_id,
            run_id,
            self._parent_run_id,
            self._agent_name,
            self._clock().isoformat(),
            continuation,
            evaluation.evaluation_run_id if evaluation else None,
            evaluation.test_case_id if evaluation else None,
            evaluation.attempt_id if evaluation else None,
        )
        context = RunObservationContext(
            session.session_id,
            run_id,
            turn_id,
            self._agent_name,
            self._parent_run_id,
            evaluation,
        )
        terminal_written = False
        with _safe_bind(self._observer, context):
            with _safe_span(
                self._observer,
                f"invoke_agent {self._agent_name}",
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": self._agent_name,
                    "gen_ai.conversation.id": session.session_id,
                    "my_code.run.id": run_id,
                    "my_code.turn.id": turn_id,
                    "my_code.turn.continuation": continuation,
                },
            ) as span:
                self._write_journal(
                    session.append_turn_started, started, "turn_started"
                )
                _safe_record(
                    self._observer, "turn.started", {"continuation": continuation}
                )
                source = (
                    self._runner.stream_continuation(session, runtime)
                    if continuation
                    else self._runner.stream(session, runtime, turn_input)  # type: ignore[arg-type]
                )
                try:
                    async for event in source:
                        if isinstance(event, AgentTurnSucceeded):
                            terminal_written = True
                            finished = TurnFinished(
                                turn_id,
                                self._clock().isoformat(),
                                "succeeded",
                                completed_steps=event.completed_steps,
                                usage=event.usage,
                            )
                            self._write_journal(
                                session.append_turn_finished, finished, "turn_finished"
                            )
                            span.set_attributes(_usage_attributes(event))
                            _safe_record(
                                self._observer,
                                "turn.completed",
                                {"outcome": "succeeded"},
                            )
                        elif isinstance(event, AgentMaxStepsReached):
                            terminal_written = True
                            finished = TurnFinished(
                                turn_id,
                                self._clock().isoformat(),
                                "max_steps",
                                completed_steps=event.completed_steps,
                                max_steps=event.max_steps,
                                usage=event.usage,
                            )
                            self._write_journal(
                                session.append_turn_finished, finished, "turn_finished"
                            )
                            span.set_attributes(_usage_attributes(event))
                            span.finish(ObservationOutcome.LIMIT)
                            _safe_record(
                                self._observer,
                                "turn.completed",
                                {"outcome": "max_steps"},
                            )
                        yield event
                except (asyncio.CancelledError, GeneratorExit):
                    if not terminal_written:
                        terminal_written = True
                        self._write_journal(
                            session.append_turn_finished,
                            TurnFinished(
                                turn_id,
                                self._clock().isoformat(),
                                "cancelled",
                            ),
                            "turn_finished",
                        )
                    span.finish(ObservationOutcome.CANCELLED)
                    _safe_record(self._observer, "turn.cancelled", {})
                    raise
                except Exception as error:
                    if not terminal_written:
                        terminal_written = True
                        self._write_journal(
                            session.append_turn_finished,
                            TurnFinished(
                                turn_id,
                                self._clock().isoformat(),
                                "failed",
                                error_type=type(error).__name__,
                            ),
                            "turn_finished",
                        )
                    span.set_attributes({"error.type": type(error).__name__})
                    span.finish(ObservationOutcome.ERROR)
                    _safe_record(
                        self._observer,
                        "turn.failed",
                        {"error_type": type(error).__name__},
                    )
                    raise

    def _write_journal(
        self,
        writer: Callable[[_JournalRecord], object],
        record: _JournalRecord,
        event_type: str,
    ) -> None:
        try:
            writer(record)
        except Exception as error:
            logger.exception("Session turn journal write failed: type=%s", event_type)
            _safe_record(
                self._observer,
                "journal.write_failed",
                {"record_type": event_type, "error_type": type(error).__name__},
            )


class InstrumentedModelClient:
    def __init__(
        self,
        client: ModelClient,
        observer: Observer,
        binding: Callable[[], ProviderBinding],
        *,
        purpose: str,
    ) -> None:
        self._client = client
        self._observer = observer
        self._binding = binding
        self._purpose = purpose

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        binding = self._binding()
        with _safe_span(
            self._observer,
            f"chat {binding.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": binding.provider_id,
                "gen_ai.request.model": binding.model,
                "my_code.model.purpose": self._purpose,
                "my_code.model.tool_count": len(request.tools),
            },
        ) as span:
            _safe_record(
                self._observer,
                "model.request",
                {"purpose": self._purpose, "request": request},
            )
            first_chunk = True
            try:
                async for event in self._client.stream(request):
                    if first_chunk:
                        first_chunk = False
                        span.add_event("gen_ai.client.first_chunk")
                    if isinstance(event.payload, ModelOutputCompleted):
                        output = event.payload.output
                        span.set_attributes(
                            {
                                "gen_ai.usage.input_tokens": (
                                    output.usage.total_input_tokens
                                ),
                                "gen_ai.usage.output_tokens": (
                                    output.usage.output_tokens
                                ),
                                "gen_ai.response.finish_reasons": (output.stop_reason,),
                            }
                        )
                        _safe_record(
                            self._observer,
                            "model.response",
                            {"purpose": self._purpose, "response": output},
                        )
                    yield event
            except (asyncio.CancelledError, GeneratorExit):
                span.finish(ObservationOutcome.CANCELLED)
                _safe_record(
                    self._observer, "model.cancelled", {"purpose": self._purpose}
                )
                raise
            except Exception as error:
                span.set_attributes({"error.type": type(error).__name__})
                span.finish(ObservationOutcome.ERROR)
                _safe_record(
                    self._observer,
                    "model.error",
                    {"purpose": self._purpose, "error_type": type(error).__name__},
                )
                raise


class InstrumentedToolExecutor:
    def __init__(self, executor: ToolExecutor, observer: Observer) -> None:
        self._executor = executor
        self._observer = observer
        self.tools = executor.tools

    def present_use(self, call: ToolCall, **kwargs: object) -> ToolUsePresentation:
        return self._executor.present_use(call, **kwargs)  # type: ignore[arg-type]

    def present_error(
        self, call: ToolCall, message: str, **kwargs: object
    ) -> ToolResultPresentation:
        return self._executor.present_error(call, message, **kwargs)  # type: ignore[arg-type]

    def cancelled_result(self, call: ToolCall, **kwargs: object) -> ToolResult:
        return self._executor.cancelled_result(call, **kwargs)  # type: ignore[arg-type]

    def is_concurrency_safe(self, call: ToolCall, **kwargs: object) -> bool:
        return self._executor.is_concurrency_safe(call, **kwargs)  # type: ignore[arg-type]

    def apply_session_updates(
        self,
        updates: tuple[PermissionUpdate, ...],
        session_mode_writer: Callable[[PermissionMode], object],
    ) -> None:
        self._executor.apply_session_updates(updates, session_mode_writer)

    async def execute(
        self,
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot | None = None,
        run_id: str | None = None,
    ) -> ToolExecutionOutcome:
        with _safe_span(
            self._observer,
            f"execute_tool {call.name}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": call.name,
                "gen_ai.tool.call.id": call.id,
            },
        ) as span:
            try:
                outcome = await self._executor.execute(call, tools=tools, run_id=run_id)
            except asyncio.CancelledError:
                span.finish(ObservationOutcome.CANCELLED)
                raise
            except Exception as error:
                span.set_attributes({"error.type": type(error).__name__})
                span.finish(ObservationOutcome.ERROR)
                raise
            if outcome.result.is_error:
                denied = outcome.result.content.startswith("Permission denied:")
                span.finish(
                    ObservationOutcome.DENIED if denied else ObservationOutcome.ERROR
                )
            _safe_record(
                self._observer,
                "tool.completed",
                {
                    "tool_name": call.name,
                    "tool_call_id": call.id,
                    "is_error": outcome.result.is_error,
                },
            )
            return outcome


class TelemetryToolInvocationAudit:
    """Permission audit whose telemetry failures never alter tool behavior."""

    def __init__(
        self, observer: Observer, delegate: ToolInvocationAudit | None = None
    ) -> None:
        self._observer = observer
        self._delegate = delegate

    async def record_permission(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        decision: PermissionDecision,
    ) -> None:
        behavior = getattr(getattr(decision, "behavior", None), "value", "unknown")
        reason = getattr(decision, "decision_reason", None)
        _safe_record(
            self._observer,
            "tool.permission",
            {
                "tool_name": call.name,
                "tool_call_id": call.id,
                "origin": invocation.origin.value,
                "behavior": behavior,
                "reason_kind": getattr(
                    getattr(reason, "kind", None), "value", "unknown"
                ),
                "reason_detail": getattr(reason, "detail", "unknown"),
            },
        )
        if self._delegate is not None:
            try:
                await self._delegate.record_permission(invocation, call, decision)
            except Exception:
                logger.exception("Permission audit delegate failed")


class InstrumentedPermissionPrompter:
    def __init__(self, prompter: PermissionPrompter, observer: Observer) -> None:
        self._prompter = prompter
        self._observer = observer

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        with _safe_span(
            self._observer,
            "tool.blocked_on_user",
            attributes={"gen_ai.tool.name": request.tool_name},
        ) as span:
            confirmation = await self._prompter.confirm(request)
            span.set_attributes({"my_code.permission.allowed": confirmation.allowed})
            return confirmation


def _usage_attributes(
    outcome: AgentTurnSucceeded | AgentMaxStepsReached,
) -> dict[str, object]:
    return {
        "my_code.agent.steps": outcome.completed_steps,
        "gen_ai.usage.input_tokens": outcome.usage.total_input_tokens,
        "gen_ai.usage.output_tokens": outcome.usage.output_tokens,
    }


def _safe_record(observer: Observer, name: str, payload: Mapping[str, object]) -> None:
    try:
        observer.record(name, payload)
    except Exception:
        logger.exception("Telemetry event failed: %s", name)


@contextmanager
def _safe_bind(
    observer: Observer, context: RunObservationContext
) -> Iterator[RunObservationContext]:
    try:
        scope = observer.bind_run(context)
        scope.__enter__()
    except Exception:
        logger.exception("Telemetry context binding failed")
        yield context
        return
    try:
        yield context
    finally:
        try:
            scope.__exit__(None, None, None)
        except Exception:
            logger.exception("Telemetry context cleanup failed")


@contextmanager
def _safe_span(
    observer: Observer,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[ObservationSpan]:
    span: ObservationSpan = NoOpSpan()
    try:
        candidate = observer.start_span(name, kind=kind, attributes=attributes)
        candidate.__enter__()
        span = candidate
    except Exception:
        logger.exception("Telemetry span start failed: %s", name)
    try:
        yield span
    except BaseException as error:
        try:
            span.__exit__(type(error), error, error.__traceback__)
        except Exception:
            logger.exception("Telemetry span cleanup failed: %s", name)
        raise
    else:
        try:
            span.__exit__(None, None, None)
        except Exception:
            logger.exception("Telemetry span cleanup failed: %s", name)


__all__ = [
    "InstrumentedAgentRunner",
    "InstrumentedModelClient",
    "InstrumentedPermissionPrompter",
    "InstrumentedToolExecutor",
    "TelemetryToolInvocationAudit",
]
