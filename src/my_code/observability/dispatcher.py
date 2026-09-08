"""进程内固定订阅的可观测性事件分发器。"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol
from uuid import uuid4

from my_code.observability.api import (
    NoOpSpan,
    ObservationOutcome,
    ObservationSpan,
    OperationTracer,
    RunObservationContext,
    SpanKind,
    bind_run_context,
)
from my_code.observability.events import ObservationEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    schema_version: int
    event_id: str
    occurred_at: str
    sequence: int
    context: RunObservationContext | None
    event: ObservationEvent


class ObservationSink(Protocol):
    def consume(self, record: ObservationRecord) -> None: ...

    def shutdown(self, timeout_millis: int) -> None: ...


class ObservationDispatcher:
    """同步冻结事件后逐一投递；任一 sink 故障都不能改变业务结果。"""

    def __init__(
        self,
        tracer: OperationTracer,
        sinks: Sequence[ObservationSink] = (),
        *,
        capture_content: bool = False,
    ) -> None:
        self._tracer = tracer
        self._sinks = tuple(sinks)
        self._sequence = 0
        self._shutdown = False
        self._capture_content = capture_content
        self._lock = RLock()

    @property
    def capture_content(self) -> bool:
        return self._capture_content

    def bind_run(
        self, context: RunObservationContext
    ) -> AbstractContextManager[RunObservationContext]:
        return bind_run_context(context)

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[ObservationSpan]:
        candidate: ObservationSpan | None = None
        span: ObservationSpan = NoOpSpan()
        try:
            candidate = self._tracer.start_span(name, kind=kind, attributes=attributes)
            candidate.__enter__()
            span = candidate
        except Exception as error:
            if candidate is not None:
                try:
                    candidate.__exit__(type(error), error, error.__traceback__)
                except Exception:
                    pass
            logger.warning("Observation operation start failed: %s", name)
        try:
            yield _SafeSpan(span)
        except BaseException as error:
            try:
                span.__exit__(type(error), error, error.__traceback__)
            except Exception:
                logger.warning("Observation operation cleanup failed: %s", name)
            raise
        else:
            try:
                span.__exit__(None, None, None)
            except Exception:
                logger.warning("Observation operation cleanup failed: %s", name)

    def publish(self, event: ObservationEvent) -> None:
        with self._lock:
            if self._shutdown:
                return
            record = ObservationRecord(
                2,
                str(uuid4()),
                datetime.now(UTC).isoformat(),
                self._sequence,
                _current_context(),
                event,
            )
            self._sequence += 1
            for sink in self._sinks:
                try:
                    sink.consume(record)
                except Exception as error:
                    logger.warning(
                        "Observation sink failed: sink=%s error_type=%s",
                        type(sink).__name__,
                        type(error).__name__,
                    )

    def shutdown(self, timeout_millis: int = 2_000) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        components = (*self._sinks, self._tracer)
        seen: set[int] = set()
        for component in components:
            if id(component) in seen:
                continue
            seen.add(id(component))
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            try:
                component.shutdown(remaining)
            except Exception as error:
                logger.warning(
                    "Observation shutdown failed: component=%s error_type=%s",
                    type(component).__name__,
                    type(error).__name__,
                )


class _SafeSpan(NoOpSpan):
    def __init__(self, delegate: ObservationSpan) -> None:
        self._delegate = delegate

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        try:
            self._delegate.set_attributes(attributes)
        except Exception:
            logger.warning("Observation span attributes failed")

    def add_event(
        self, name: str, attributes: Mapping[str, object] | None = None
    ) -> None:
        try:
            self._delegate.add_event(name, attributes)
        except Exception:
            logger.warning("Observation span event failed")

    def finish(self, outcome: ObservationOutcome = ObservationOutcome.OK) -> None:
        try:
            self._delegate.finish(outcome)
        except Exception:
            logger.warning("Observation span finish failed")


def _current_context() -> RunObservationContext | None:
    from my_code.observability.api import current_run_context

    return current_run_context()


__all__ = ["ObservationDispatcher", "ObservationRecord", "ObservationSink"]
