"""Technology-neutral observability contracts used by runtime domains."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self

from my_code.foundation.json import JsonValue


class SpanKind(StrEnum):
    INTERNAL = "internal"
    CLIENT = "client"


class ObservationOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    DENIED = "denied"
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    evaluation_run_id: str | None = None
    test_case_id: str | None = None
    attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunObservationContext:
    session_id: str
    run_id: str
    invocation_id: str
    agent_name: str
    parent_run_id: str | None = None
    evaluation: EvaluationContext | None = None


class ObservationSpan(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool: ...

    def set_attributes(self, attributes: Mapping[str, object]) -> None: ...

    def add_event(
        self, name: str, attributes: Mapping[str, object] | None = None
    ) -> None: ...

    def finish(self, outcome: ObservationOutcome = ObservationOutcome.OK) -> None: ...


class Observer(Protocol):
    def bind_run(
        self, context: RunObservationContext
    ) -> AbstractContextManager[RunObservationContext]: ...

    def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, object] | None = None,
    ) -> ObservationSpan: ...

    def record(self, event_type: str, payload: Mapping[str, object]) -> None: ...

    def shutdown(self, timeout_millis: int = 2_000) -> None: ...


_RUN_CONTEXT: ContextVar[RunObservationContext | None] = ContextVar(
    "my_code_observation_run", default=None
)


def current_run_context() -> RunObservationContext | None:
    return _RUN_CONTEXT.get()


class NoOpSpan:
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        del attributes

    def add_event(
        self, name: str, attributes: Mapping[str, object] | None = None
    ) -> None:
        del name, attributes

    def finish(self, outcome: ObservationOutcome = ObservationOutcome.OK) -> None:
        del outcome


class NoOpObserver:
    @contextmanager
    def bind_run(
        self, context: RunObservationContext
    ) -> Iterator[RunObservationContext]:
        token: Token[RunObservationContext | None] = _RUN_CONTEXT.set(context)
        try:
            yield context
        finally:
            _RUN_CONTEXT.reset(token)

    def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, object] | None = None,
    ) -> NoOpSpan:
        del name, kind, attributes
        return NoOpSpan()

    def record(self, event_type: str, payload: Mapping[str, object]) -> None:
        del event_type, payload

    def shutdown(self, timeout_millis: int = 2_000) -> None:
        del timeout_millis


def evaluation_payload(context: EvaluationContext | None) -> JsonValue:
    if context is None:
        return None
    return {
        "evaluation_run_id": context.evaluation_run_id,
        "test_case_id": context.test_case_id,
        "attempt_id": context.attempt_id,
    }


__all__ = [
    "EvaluationContext",
    "NoOpObserver",
    "NoOpSpan",
    "ObservationOutcome",
    "ObservationSpan",
    "Observer",
    "RunObservationContext",
    "SpanKind",
    "current_run_context",
]
