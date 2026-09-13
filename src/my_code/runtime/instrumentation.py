"""Failure-isolated observability adapters at the runtime composition boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TypeVar, cast
from uuid import uuid4

from my_code.agent.events import AgentEvent
from my_code.agent.models import (
    AgentInvocationOutcome,
    AgentInvocationSucceeded,
    AgentMaxStepsReached,
    AgentTurnInput,
    PendingInputSource,
    UserTurnInput,
)
from my_code.agent.runner import AgentRunner, InteractiveAgentRunner
from my_code.context.session_cache import SessionContextCache
from my_code.conversation.models import ToolCall, ToolResult
from my_code.conversation.presentation import ToolResultPresentation
from my_code.model.client import ModelClient
from my_code.model.events import (
    ModelOutputCompleted,
    ModelStreamEvent,
    ModelTextCompleted,
    ModelTextDelta,
)
from my_code.model.primitives import ProviderBinding
from my_code.model.request import ModelRequest
from my_code.observability.api import (
    EvaluationContext,
    ObservationOutcome,
    RunObservationContext,
    SpanKind,
)
from my_code.observability.dispatcher import ObservationDispatcher
from my_code.observability.events import (
    EventOutcome,
    JournalWriteFailed,
    ModelRequestFinished,
    ModelRequestStarted,
    ModelResponseReceived,
    SensitiveContent,
    ToolExecutionFinished,
    ToolExecutionStarted,
    ToolPermissionEvaluated,
)
from my_code.observability.events import (
    InvocationFinished as InvocationFinishedEvent,
)
from my_code.observability.events import InvocationStarted as InvocationStartedEvent
from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionDecision,
    PermissionMode,
    PermissionPrompt,
    PermissionPrompter,
    PermissionUpdate,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.sessions.models import InvocationFinished, InvocationStarted
from my_code.sessions.session import Session
from my_code.tools.base import ConcurrencyAssessment
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import ToolExposureSnapshot
from my_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from my_code.tools.invocation import ToolInvocation, ToolInvocationAudit
from my_code.tools.presentation import ToolUsePresentation

logger = logging.getLogger(__name__)
_JournalRecord = TypeVar("_JournalRecord", InvocationStarted, InvocationFinished)


class InstrumentedAgentRunner:
    def __init__(
        self,
        runner: AgentRunner | InteractiveAgentRunner,
        observations: ObservationDispatcher,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        agent_name: str = "main",
        evaluation: EvaluationContext | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = runner
        self._observations = observations
        self._run_id = run_id
        self._parent_run_id = parent_run_id
        self._agent_name = agent_name
        self._evaluation = evaluation
        self._clock = clock or (lambda: datetime.now(UTC))

    async def submit(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput | Sequence[UserTurnInput],
        pending_source: PendingInputSource | None = None,
    ) -> AgentInvocationOutcome:
        outcome: AgentInvocationOutcome | None = None
        async for event in self.stream(
            session, runtime, turn_input, pending_source=pending_source
        ):
            if isinstance(event, (AgentInvocationSucceeded, AgentMaxStepsReached)):
                outcome = event
        if outcome is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return outcome

    def stream(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput | Sequence[UserTurnInput],
        pending_source: PendingInputSource | None = None,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(
            session,
            runtime,
            turn_input,
            continuation=False,
            pending_source=pending_source,
        )

    def stream_continuation(
        self,
        session: Session,
        runtime: SessionContextCache,
        pending_source: PendingInputSource | None = None,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(
            session,
            runtime,
            None,
            continuation=True,
            pending_source=pending_source,
        )

    async def _stream(
        self,
        session: Session,
        runtime: SessionContextCache,
        turn_input: AgentTurnInput | Sequence[UserTurnInput] | None,
        *,
        continuation: bool,
        pending_source: PendingInputSource | None,
    ) -> AsyncIterator[AgentEvent]:
        invocation_id = str(uuid4())
        started_at = time.monotonic()
        run_id = self._run_id or session.run_id
        evaluation = self._evaluation
        started = InvocationStarted(
            invocation_id,
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
            invocation_id,
            self._agent_name,
            self._parent_run_id,
            evaluation,
        )
        terminal_written = False
        with self._observations.bind_run(context):
            with self._observations.operation(
                f"invoke_agent {self._agent_name}",
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": self._agent_name,
                    "gen_ai.conversation.id": session.session_id,
                    "my_code.run.id": run_id,
                    "my_code.invocation.id": invocation_id,
                    "my_code.invocation.continuation": continuation,
                },
            ) as span:
                self._write_journal(
                    session.append_invocation_started, started, "invocation_started"
                )
                self._observations.publish(
                    InvocationStartedEvent(continuation=continuation)
                )
                if pending_source is not None:
                    interactive = cast(InteractiveAgentRunner, self._runner)
                    source = (
                        interactive.stream_continuation(
                            session, runtime, pending_source=pending_source
                        )
                        if continuation
                        else interactive.stream(
                            session,
                            runtime,
                            turn_input,  # type: ignore[arg-type]
                            pending_source=pending_source,
                        )
                    )
                else:
                    source = (
                        self._runner.stream_continuation(session, runtime)
                        if continuation
                        else self._runner.stream(
                            session,
                            runtime,
                            turn_input,  # type: ignore[arg-type]
                        )
                    )
                try:
                    async for event in source:
                        if isinstance(event, AgentInvocationSucceeded):
                            terminal_written = True
                            finished = InvocationFinished(
                                invocation_id,
                                self._clock().isoformat(),
                                "succeeded",
                                completed_steps=event.completed_steps,
                                usage=event.usage,
                            )
                            self._write_journal(
                                session.append_invocation_finished,
                                finished,
                                "invocation_finished",
                            )
                            span.set_attributes(_usage_attributes(event))
                            span.finish(ObservationOutcome.OK)
                            self._observations.publish(
                                InvocationFinishedEvent(
                                    EventOutcome.OK,
                                    (time.monotonic() - started_at) * 1000,
                                    completed_steps=event.completed_steps,
                                )
                            )
                        elif isinstance(event, AgentMaxStepsReached):
                            terminal_written = True
                            finished = InvocationFinished(
                                invocation_id,
                                self._clock().isoformat(),
                                "max_steps",
                                completed_steps=event.completed_steps,
                                max_steps=event.max_steps,
                                usage=event.usage,
                            )
                            self._write_journal(
                                session.append_invocation_finished,
                                finished,
                                "invocation_finished",
                            )
                            span.set_attributes(_usage_attributes(event))
                            span.finish(ObservationOutcome.LIMIT)
                            self._observations.publish(
                                InvocationFinishedEvent(
                                    EventOutcome.LIMIT,
                                    (time.monotonic() - started_at) * 1000,
                                    completed_steps=event.completed_steps,
                                    max_steps=event.max_steps,
                                )
                            )
                        yield event
                except (asyncio.CancelledError, GeneratorExit):
                    if not terminal_written:
                        terminal_written = True
                        self._write_journal(
                            session.append_invocation_finished,
                            InvocationFinished(
                                invocation_id,
                                self._clock().isoformat(),
                                "cancelled",
                            ),
                            "invocation_finished",
                        )
                        span.finish(ObservationOutcome.CANCELLED)
                        self._observations.publish(
                            InvocationFinishedEvent(
                                EventOutcome.CANCELLED,
                                (time.monotonic() - started_at) * 1000,
                            )
                        )
                    raise
                except Exception as error:
                    if not terminal_written:
                        terminal_written = True
                        self._write_journal(
                            session.append_invocation_finished,
                            InvocationFinished(
                                invocation_id,
                                self._clock().isoformat(),
                                "failed",
                                error_type=type(error).__name__,
                            ),
                            "invocation_finished",
                        )
                    span.set_attributes({"error.type": type(error).__name__})
                    span.finish(ObservationOutcome.ERROR)
                    self._observations.publish(
                        InvocationFinishedEvent(
                            EventOutcome.ERROR,
                            (time.monotonic() - started_at) * 1000,
                            error_type=type(error).__name__,
                        )
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
            logger.exception(
                "Session invocation journal write failed: type=%s", event_type
            )
            self._observations.publish(
                JournalWriteFailed(event_type, type(error).__name__)
            )


class InstrumentedModelClient:
    def __init__(
        self,
        client: ModelClient,
        observations: ObservationDispatcher,
        binding: Callable[[], ProviderBinding],
        *,
        purpose: str,
    ) -> None:
        self._client = client
        self._observations = observations
        self._binding = binding
        self._purpose = purpose

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        binding = self._binding()
        started = time.monotonic()
        metadata: dict[str, object] = {
            "purpose": self._purpose,
            "provider": binding.provider_id,
            "model": binding.model,
        }
        if request.identity is not None:
            metadata.update(
                request_id=request.identity.request_id,
                step=request.identity.step,
                attempt=request.identity.attempt,
                purpose=request.identity.purpose,
            )
        with self._observations.operation(
            f"chat {binding.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": binding.provider_id,
                "gen_ai.request.model": binding.model,
                "my_code.model.purpose": metadata["purpose"],
                "my_code.model.tool_count": len(request.tools),
                **(
                    {
                        "my_code.request.id": request.identity.request_id,
                        "my_code.request.step": request.identity.step,
                        "my_code.request.attempt": request.identity.attempt,
                    }
                    if request.identity is not None
                    else {}
                ),
            },
        ) as span:
            self._observations.publish(
                ModelRequestStarted(
                    purpose=str(metadata["purpose"]),
                    provider=binding.provider_id,
                    model=binding.model,
                    request_id=_optional_str(metadata.get("request_id")),
                    step=_optional_int(metadata.get("step")),
                    attempt=_optional_int(metadata.get("attempt")),
                    content=_sensitive_content(self._observations, request),
                )
            )
            first_chunk = True
            first_text = True
            completed = False
            try:
                async for event in self._client.stream(request):
                    if first_chunk:
                        first_chunk = False
                        span.add_event("gen_ai.client.first_chunk")
                        metadata["first_event_ms"] = (time.monotonic() - started) * 1000
                    if (
                        first_text
                        and isinstance(
                            event.payload, (ModelTextDelta, ModelTextCompleted)
                        )
                        and event.payload.text
                    ):
                        first_text = False
                        metadata["first_text_ms"] = (time.monotonic() - started) * 1000
                        span.add_event("my_code.client.first_text")
                    if isinstance(event.payload, ModelOutputCompleted):
                        completed = True
                        output = event.payload.output
                        metadata.update(
                            input_tokens=output.usage.input_tokens,
                            output_tokens=output.usage.output_tokens,
                            cache_read_tokens=output.usage.cache_read_input_tokens,
                            cache_creation_tokens=output.usage.cache_creation_input_tokens,
                            provider_reported=output.usage.provider_reported,
                            stop_reason=output.stop_reason,
                        )
                        span.set_attributes(
                            {
                                "gen_ai.usage.input_tokens": (
                                    output.usage.total_input_tokens
                                ),
                                "gen_ai.usage.output_tokens": (
                                    output.usage.output_tokens
                                ),
                                "gen_ai.response.finish_reasons": (output.stop_reason,),
                                "my_code.usage.cache_read_tokens": (
                                    output.usage.cache_read_input_tokens
                                ),
                                "my_code.usage.cache_creation_tokens": (
                                    output.usage.cache_creation_input_tokens
                                ),
                            }
                        )
                        self._observations.publish(
                            ModelResponseReceived(
                                purpose=str(metadata["purpose"]),
                                provider=binding.provider_id,
                                model=binding.model,
                                input_tokens=output.usage.input_tokens,
                                output_tokens=output.usage.output_tokens,
                                cache_read_tokens=(
                                    output.usage.cache_read_input_tokens
                                ),
                                cache_creation_tokens=(
                                    output.usage.cache_creation_input_tokens
                                ),
                                provider_reported=output.usage.provider_reported,
                                stop_reason=output.stop_reason,
                                request_id=_optional_str(metadata.get("request_id")),
                                step=_optional_int(metadata.get("step")),
                                attempt=_optional_int(metadata.get("attempt")),
                                first_event_ms=_optional_float(
                                    metadata.get("first_event_ms")
                                ),
                                first_text_ms=_optional_float(
                                    metadata.get("first_text_ms")
                                ),
                                content=_sensitive_content(self._observations, output),
                            )
                        )
                    yield event
            except (asyncio.CancelledError, GeneratorExit):
                span.finish(ObservationOutcome.CANCELLED)
                self._observations.publish(
                    _model_finished_event(
                        metadata,
                        binding,
                        EventOutcome.CANCELLED,
                        (time.monotonic() - started) * 1000,
                    )
                )
                raise
            except Exception as error:
                span.set_attributes({"error.type": type(error).__name__})
                span.finish(ObservationOutcome.ERROR)
                self._observations.publish(
                    _model_finished_event(
                        metadata,
                        binding,
                        EventOutcome.ERROR,
                        (time.monotonic() - started) * 1000,
                        error_type=type(error).__name__,
                    )
                )
                raise
            else:
                # response 仅表示收到了输出；流自然结束才记为 completed。
                if not completed:
                    span.finish(ObservationOutcome.ERROR)
                self._observations.publish(
                    _model_finished_event(
                        metadata,
                        binding,
                        EventOutcome.OK if completed else EventOutcome.MISSING_OUTPUT,
                        (time.monotonic() - started) * 1000,
                    )
                )


class InstrumentedToolExecutor:
    def __init__(
        self,
        executor: ToolExecutor,
        observations: ObservationDispatcher,
    ) -> None:
        self._executor = executor
        self._observations = observations
        self.tools = executor.tools

    def permission_snapshot(self) -> PermissionPolicy:
        return self._executor.permission_snapshot()

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

    def concurrency_assessment(
        self, call: ToolCall, **kwargs: object
    ) -> ConcurrencyAssessment:
        return self._executor.concurrency_assessment(call, **kwargs)  # type: ignore[arg-type]

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
        permission_policy: PermissionPolicy | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> ToolExecutionOutcome:
        started = time.monotonic()
        metadata: dict[str, object] = {
            "tool_name": call.name,
            "tool_call_id": call.id,
            "input_sha256": hashlib.sha256(
                json.dumps(call.input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        with self._observations.operation(
            f"execute_tool {call.name}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": call.name,
                "gen_ai.tool.call.id": call.id,
            },
        ) as span:
            self._observations.publish(
                ToolExecutionStarted(call.name, call.id, str(metadata["input_sha256"]))
            )
            try:
                outcome = await self._executor.execute(
                    call,
                    tools=tools,
                    permission_policy=permission_policy,
                    run_id=run_id,
                    session_id=session_id,
                    root_session_id=root_session_id,
                )
            except asyncio.CancelledError:
                span.finish(ObservationOutcome.CANCELLED)
                self._observations.publish(
                    ToolExecutionFinished(
                        call.name,
                        call.id,
                        EventOutcome.CANCELLED,
                        (time.monotonic() - started) * 1000,
                        False,
                        input_sha256=str(metadata["input_sha256"]),
                    )
                )
                raise
            except Exception as error:
                span.set_attributes({"error.type": type(error).__name__})
                span.finish(ObservationOutcome.ERROR)
                self._observations.publish(
                    ToolExecutionFinished(
                        call.name,
                        call.id,
                        EventOutcome.ERROR,
                        (time.monotonic() - started) * 1000,
                        True,
                        input_sha256=str(metadata["input_sha256"]),
                        error_type=type(error).__name__,
                    )
                )
                raise
            if outcome.result.is_error:
                # 权限拒绝由结构化 permission 事件表达，不从模型可见文本猜测。
                span.finish(ObservationOutcome.ERROR)
            self._observations.publish(
                ToolExecutionFinished(
                    call.name,
                    call.id,
                    (
                        EventOutcome.ERROR
                        if outcome.result.is_error
                        else EventOutcome.OK
                    ),
                    (time.monotonic() - started) * 1000,
                    outcome.result.is_error,
                    input_sha256=str(metadata["input_sha256"]),
                )
            )
            return outcome


class TelemetryToolInvocationAudit:
    """Permission audit whose telemetry failures never alter tool behavior."""

    def __init__(
        self,
        observations: ObservationDispatcher,
        delegate: ToolInvocationAudit | None = None,
        execution_backend: str = "unknown",
    ) -> None:
        self._observations = observations
        self._delegate = delegate
        self._execution_backend = execution_backend

    async def record_permission(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        decision: PermissionDecision,
    ) -> None:
        behavior = getattr(getattr(decision, "behavior", None), "value", "unknown")
        reason = getattr(decision, "decision_reason", None)
        authority = call.input.get("sandbox_permissions", "use_default")
        self._observations.publish(
            ToolPermissionEvaluated(
                tool_name=call.name,
                tool_call_id=call.id,
                origin=invocation.origin.value,
                behavior=behavior,
                reason_kind=getattr(getattr(reason, "kind", None), "value", "unknown"),
                authority=str(authority),
                execution_backend=(
                    "local"
                    if authority == "require_escalated"
                    else self._execution_backend
                ),
            )
        )
        if self._delegate is not None:
            try:
                await self._delegate.record_permission(invocation, call, decision)
            except Exception:
                logger.exception("Permission audit delegate failed")


class InstrumentedPermissionPrompter:
    def __init__(
        self, prompter: PermissionPrompter, observations: ObservationDispatcher
    ) -> None:
        self._prompter = prompter
        self._observations = observations

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        with self._observations.operation(
            "tool.blocked_on_user",
            attributes={"gen_ai.tool.name": request.tool_name},
        ) as span:
            confirmation = await self._prompter.confirm(request)
            span.set_attributes({"my_code.permission.allowed": confirmation.allowed})
            return confirmation


def _usage_attributes(
    outcome: AgentInvocationSucceeded | AgentMaxStepsReached,
) -> dict[str, object]:
    return {
        "my_code.agent.steps": outcome.completed_steps,
        "gen_ai.usage.input_tokens": outcome.usage.total_input_tokens,
        "gen_ai.usage.output_tokens": outcome.usage.output_tokens,
    }


def _model_finished_event(
    metadata: Mapping[str, object],
    binding: ProviderBinding,
    outcome: EventOutcome,
    duration_ms: float,
    *,
    error_type: str | None = None,
) -> ModelRequestFinished:
    return ModelRequestFinished(
        purpose=str(metadata["purpose"]),
        provider=binding.provider_id,
        model=binding.model,
        outcome=outcome,
        duration_ms=duration_ms,
        request_id=_optional_str(metadata.get("request_id")),
        step=_optional_int(metadata.get("step")),
        attempt=_optional_int(metadata.get("attempt")),
        error_type=error_type,
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _sensitive_content(
    observations: ObservationDispatcher, value: object
) -> SensitiveContent | None:
    if not observations.capture_content:
        return None
    return SensitiveContent(_observation_json(value))


def _observation_json(value: object):
    """显式 opt-in 时才构造正文，排除 opaque continuation replay。"""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _observation_json(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): _observation_json(item)
            for key, item in value.items()
            if str(key) != "continuation"
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_observation_json(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _observation_json(getattr(value, item.name))
            for item in fields(value)
            if item.name != "continuation"
        }
    return repr(value)


__all__ = [
    "InstrumentedAgentRunner",
    "InstrumentedModelClient",
    "InstrumentedPermissionPrompter",
    "InstrumentedToolExecutor",
    "TelemetryToolInvocationAudit",
]
