"""OpenTelemetry implementation isolated from runtime domain packages."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from types import TracebackType
from typing import Self

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind as OtelSpanKind
from opentelemetry.trace import Status, StatusCode
from opentelemetry.util.types import AttributeValue

from my_code.observability.api import (
    _RUN_CONTEXT,
    ObservationOutcome,
    RunObservationContext,
    SpanKind,
    current_run_context,
)

logger = logging.getLogger(__name__)
_CONTENT_LIMIT = 16 * 1024
_SENSITIVE_KEYS = {
    "content",
    "input",
    "output",
    "payload",
    "prompt",
    "request",
    "response",
    "result",
    "system_prompt",
    "tool_input",
}


class OpenTelemetrySpan:
    def __init__(
        self,
        observer: OpenTelemetryObserver,
        name: str,
        kind: SpanKind,
        attributes: Mapping[str, object] | None,
    ) -> None:
        self._observer = observer
        self._name = name
        self._kind = kind
        self._attributes = attributes
        self._scope: object | None = None
        self._span: trace.Span | None = None
        self._started = 0.0
        self._finished = False

    def __enter__(self) -> Self:
        context = current_run_context()
        attributes = _context_attributes(context) if context is not None else {}
        attributes.update(_otel_attributes(self._attributes or {}))
        scope = self._observer.tracer.start_as_current_span(
            self._name,
            kind=(
                OtelSpanKind.CLIENT
                if self._kind is SpanKind.CLIENT
                else OtelSpanKind.INTERNAL
            ),
            attributes=attributes,
        )
        self._scope = scope
        self._span = scope.__enter__()
        self._started = time.monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc is not None and self._span is not None:
            self._span.record_exception(exc)
            self._span.set_attribute("error.type", type(exc).__name__)
            if type(exc).__name__ == "CancelledError":
                self.finish(ObservationOutcome.CANCELLED)
            else:
                self.finish(ObservationOutcome.ERROR)
        else:
            self.finish()
        if self._scope is not None:
            return bool(self._scope.__exit__(exc_type, exc, traceback))  # type: ignore[attr-defined]
        return False

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        if self._span is not None:
            for key, value in _otel_attributes(attributes).items():
                self._span.set_attribute(key, value)

    def add_event(
        self, name: str, attributes: Mapping[str, object] | None = None
    ) -> None:
        if self._span is not None:
            self._span.add_event(name, _otel_attributes(attributes or {}))

    def finish(self, outcome: ObservationOutcome = ObservationOutcome.OK) -> None:
        if self._finished:
            return
        self._finished = True
        if self._span is not None:
            self._span.set_attribute("my_code.outcome", outcome.value)
            if outcome is ObservationOutcome.ERROR:
                self._span.set_status(Status(StatusCode.ERROR))
            elif outcome is ObservationOutcome.OK:
                self._span.set_status(Status(StatusCode.OK))
        duration = max(0.0, time.monotonic() - self._started)
        self._observer.record_duration(self._name, duration, outcome)


class OpenTelemetryObserver:
    def __init__(
        self,
        tracer_provider: TracerProvider,
        meter_provider: MeterProvider,
        *,
        capture_content: bool,
    ) -> None:
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self.tracer = tracer_provider.get_tracer("my_code", "0.1.0")
        meter = meter_provider.get_meter("my_code", "0.1.0")
        self._duration = meter.create_histogram(
            "my_code.operation.duration",
            unit="s",
            description="Runtime operation duration",
        )
        self._capture_content = capture_content

    @contextmanager
    def bind_run(
        self, context: RunObservationContext
    ) -> Iterator[RunObservationContext]:
        token = _RUN_CONTEXT.set(context)
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
    ) -> OpenTelemetrySpan:
        return OpenTelemetrySpan(self, name, kind, attributes)

    def record(self, event_type: str, payload: Mapping[str, object]) -> None:
        span = trace.get_current_span()
        if not span.is_recording():
            return
        attributes: dict[str, object] = {"my_code.event.type": event_type}
        for key, value in payload.items():
            if _is_scalar(value) and key.casefold() not in _SENSITIVE_KEYS:
                attributes[f"my_code.event.{key}"] = value
        if self._capture_content:
            encoded = json.dumps(
                _captured_content(payload), ensure_ascii=False
            ).encode()
            attributes["my_code.event.content.bytes"] = len(encoded)
            attributes["my_code.event.content.sha256"] = hashlib.sha256(
                encoded
            ).hexdigest()
            attributes["my_code.event.content"] = encoded[:_CONTENT_LIMIT].decode(
                "utf-8", errors="replace"
            )
            attributes["my_code.event.content.truncated"] = (
                len(encoded) > _CONTENT_LIMIT
            )
        span.add_event(event_type, _otel_attributes(attributes))

    def record_duration(
        self, name: str, duration: float, outcome: ObservationOutcome
    ) -> None:
        self._duration.record(
            duration,
            {"my_code.operation.name": name, "my_code.outcome": outcome.value},
        )

    def shutdown(self, timeout_millis: int = 2_000) -> None:
        self._tracer_provider.force_flush(timeout_millis)
        self._meter_provider.force_flush(timeout_millis)
        self._tracer_provider.shutdown()
        self._meter_provider.shutdown()

    @staticmethod
    def trace_identifiers() -> tuple[str | None, str | None]:
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None, None
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def build_otel_observer(
    *,
    service_name: str,
    service_version: str,
    capture_content: bool,
) -> OpenTelemetryObserver:
    # Importing exporters lazily keeps the API usable in unit tests without a
    # configured endpoint and prevents any network work before bootstrap.
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create(
        {"service.name": service_name, "service.version": service_version}
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=(metric_reader,))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    return OpenTelemetryObserver(
        tracer_provider, meter_provider, capture_content=capture_content
    )


def _context_attributes(context: RunObservationContext) -> dict[str, AttributeValue]:
    values: dict[str, AttributeValue] = {
        "gen_ai.agent.name": context.agent_name,
        "gen_ai.conversation.id": context.session_id,
        "my_code.run.id": context.run_id,
        "my_code.turn.id": context.turn_id,
    }
    if context.parent_run_id is not None:
        values["my_code.parent_run.id"] = context.parent_run_id
    if context.evaluation is not None:
        for key, value in {
            "my_code.evaluation.run.id": context.evaluation.evaluation_run_id,
            "my_code.evaluation.case.id": context.evaluation.test_case_id,
            "my_code.evaluation.attempt.id": context.evaluation.attempt_id,
        }.items():
            if value is not None:
                values[key] = value
    return values


def _is_scalar(value: object) -> bool:
    return isinstance(value, (bool, int, float, str))


def _captured_content(value: object) -> object:
    """Project explicit content events while excluding opaque provider replay."""

    if value is None or _is_scalar(value):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _captured_content(item)
            for key, item in value.items()
            if str(key) != "continuation"
        }
    if isinstance(value, (tuple, list)):
        return [_captured_content(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _captured_content(getattr(value, item.name))
            for item in fields(value)
            if item.name != "continuation"
        }
    return repr(value)


def _otel_attributes(values: Mapping[str, object]) -> dict[str, AttributeValue]:
    result: dict[str, AttributeValue] = {}
    for key, value in values.items():
        if isinstance(value, (bool, int, float, str)):
            result[key] = value
        elif isinstance(value, (tuple, list)) and all(
            _is_scalar(item) for item in value
        ):
            result[key] = tuple(str(item) for item in value)
    return result


__all__ = ["OpenTelemetryObserver", "build_otel_observer"]
