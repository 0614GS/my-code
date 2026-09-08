"""OpenTelemetry implementation isolated from runtime domain packages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from enum import StrEnum
from threading import Thread
from types import TracebackType
from typing import Self

from opentelemetry import metrics, trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind as OtelSpanKind
from opentelemetry.trace import Status, StatusCode
from opentelemetry.util.types import AttributeValue

from my_code.observability.api import (
    ObservationOutcome,
    RunObservationContext,
    SpanKind,
    current_run_context,
)
from my_code.observability.dispatcher import ObservationRecord
from my_code.observability.events import (
    EventOutcome,
    InvocationFinished,
    ModelRequestFinished,
    ModelResponseReceived,
    ToolExecutionFinished,
    event_attributes,
    sensitive_content,
)
from my_code.version import __version__

logger = logging.getLogger(__name__)
_CONTENT_LIMIT = 16 * 1024


class OpenTelemetrySpan:
    def __init__(
        self,
        observer: OpenTelemetryBackend,
        name: str,
        kind: SpanKind,
        attributes: Mapping[str, object] | None,
    ) -> None:
        self._observer = observer
        self._name = name
        self._kind = kind
        self._attributes = attributes
        self._scope: AbstractContextManager[trace.Span] | None = None
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
            # 异常正文可能包含凭据或用户内容，禁止 SDK 自动记录及填充 status 描述。
            record_exception=False,
            set_status_on_exception=False,
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
        try:
            if (
                exc is not None
                and self._span is not None
                and not (self._finished and isinstance(exc, GeneratorExit))
            ):
                self._span.set_attribute("error.type", type(exc).__name__)
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                    self.finish(ObservationOutcome.CANCELLED)
                else:
                    self.finish(ObservationOutcome.ERROR)
            else:
                self.finish()
        finally:
            # 指标导出失败也必须归还当前 context，避免后续调用挂错父 span。
            if self._scope is not None:
                self._scope.__exit__(exc_type, exc, traceback)
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


class OpenTelemetryBackend:
    def __init__(
        self,
        tracer_provider: TracerProvider,
        meter_provider: MeterProvider,
        *,
        capture_content: bool,
        logger_provider: LoggerProvider | None = None,
    ) -> None:
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._logger_provider = logger_provider
        self.tracer = tracer_provider.get_tracer("my_code", __version__)
        self._event_logger = (
            logger_provider.get_logger("my_code", __version__)
            if logger_provider is not None
            else None
        )
        meter = meter_provider.get_meter("my_code", __version__)
        self._duration = meter.create_histogram(
            "my_code.operation.duration",
            unit="s",
            description="Runtime operation duration",
        )
        self._event_count = meter.create_counter(
            "my_code.event.count", description="Published observation events"
        )
        self._model_tokens = meter.create_counter(
            "gen_ai.client.token.usage",
            unit="token",
            description="Provider-reported model token usage",
        )
        self._model_duration = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s"
        )
        self._tool_duration = meter.create_histogram(
            "my_code.tool.execution.duration", unit="s"
        )
        self._invocation_duration = meter.create_histogram(
            "my_code.invocation.duration", unit="s"
        )
        self._capture_content = capture_content
        self._shutdown_threads: tuple[Thread, ...] | None = None

    def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, object] | None = None,
    ) -> OpenTelemetrySpan:
        return OpenTelemetrySpan(self, name, kind, attributes)

    def consume(self, record: ObservationRecord) -> None:
        event_type = record.event.name
        payload = event_attributes(record.event)
        span = trace.get_current_span()
        attributes: dict[str, object] = {
            "my_code.event.id": record.event_id,
            "my_code.event.type": event_type,
            "my_code.event.sequence": record.sequence,
        }
        if record.context is not None:
            attributes.update(_context_attributes(record.context))
        for key, value in payload.items():
            attributes[f"my_code.event.{key}"] = value
        content = sensitive_content(record.event)
        if self._capture_content and content is not None:
            encoded = json.dumps(content.value, ensure_ascii=False).encode()
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
        otel_attributes = _otel_attributes(attributes)
        if span.is_recording():
            span.add_event(event_type, otel_attributes)
            _enrich_span(span, record.event)
        if self._event_logger is not None:
            self._event_logger.emit(
                timestamp=_timestamp_ns(record.occurred_at),
                severity_number=SeverityNumber.INFO,
                severity_text="INFO",
                body=event_type,
                event_name=event_type,
                attributes=otel_attributes,
            )
        self._record_event_metrics(record)

    def _record_event_metrics(self, record: ObservationRecord) -> None:
        event = record.event
        dimensions = _metric_dimensions(event)
        self._event_count.add(1, {"event.name": event.name, **dimensions})
        if isinstance(event, ModelResponseReceived) and event.provider_reported:
            for token_type, value in (
                ("input", event.input_tokens),
                ("output", event.output_tokens),
                ("cache_read", event.cache_read_tokens),
                ("cache_creation", event.cache_creation_tokens),
            ):
                self._model_tokens.add(
                    value, {**dimensions, "gen_ai.token.type": token_type}
                )
        elif isinstance(event, ModelRequestFinished):
            self._model_duration.record(event.duration_ms / 1000, dimensions)
        elif isinstance(event, ToolExecutionFinished):
            self._tool_duration.record(event.duration_ms / 1000, dimensions)
        elif isinstance(event, InvocationFinished):
            self._invocation_duration.record(event.duration_ms / 1000, dimensions)

    def record_duration(
        self, name: str, duration: float, outcome: ObservationOutcome
    ) -> None:
        self._duration.record(
            duration,
            {"my_code.operation.name": name, "my_code.outcome": outcome.value},
        )

    def shutdown(self, timeout_millis: int = 2_000) -> None:
        """整个关闭过程共用预算；不等待失联 exporter 无限阻塞主线程。"""

        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        if self._shutdown_threads is None:
            providers = [self._tracer_provider, self._meter_provider]
            if self._logger_provider is not None:
                providers.append(self._logger_provider)
            self._shutdown_threads = tuple(
                Thread(target=_shutdown_provider, args=(provider,), daemon=True)
                for provider in providers
            )
            for thread in self._shutdown_threads:
                thread.start()
        for thread in self._shutdown_threads:
            thread.join(max(0, deadline - time.monotonic()))


def _shutdown_provider(
    provider: TracerProvider | MeterProvider | LoggerProvider,
) -> None:
    try:
        # SDK shutdown 自身负责最后一批导出，不额外重复 force_flush。
        provider.shutdown()
    except Exception:
        logger.warning("OpenTelemetry shutdown failed")


def build_otel_backend(
    *,
    service_name: str,
    service_version: str,
    capture_content: bool,
    enable_traces: bool,
    enable_metrics: bool,
    enable_logs: bool,
) -> OpenTelemetryBackend:
    resource = Resource.create(
        {"service.name": service_name, "service.version": service_version}
    )
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    if enable_traces:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
    metric_readers: tuple[PeriodicExportingMetricReader, ...] = ()
    if enable_metrics:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        metric_readers = (PeriodicExportingMetricReader(OTLPMetricExporter()),)
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=metric_readers,
        shutdown_on_exit=False,
    )
    if enable_metrics:
        metrics.set_meter_provider(meter_provider)
    logger_provider: LoggerProvider | None = None
    if enable_logs:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter())
        )
    return OpenTelemetryBackend(
        tracer_provider,
        meter_provider,
        capture_content=capture_content,
        logger_provider=logger_provider,
    )


def _context_attributes(context: RunObservationContext) -> dict[str, AttributeValue]:
    values: dict[str, AttributeValue] = {
        "gen_ai.agent.name": context.agent_name,
        "gen_ai.conversation.id": context.session_id,
        "my_code.run.id": context.run_id,
        "my_code.invocation.id": context.invocation_id,
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


def _enrich_span(span: trace.Span, event: object) -> None:
    if isinstance(event, ModelResponseReceived):
        span.set_attributes(
            {
                "gen_ai.usage.input_tokens": event.input_tokens
                + event.cache_read_tokens
                + event.cache_creation_tokens,
                "gen_ai.usage.output_tokens": event.output_tokens,
                "gen_ai.response.finish_reasons": (event.stop_reason,),
                "my_code.usage.cache_read_tokens": event.cache_read_tokens,
                "my_code.usage.cache_creation_tokens": event.cache_creation_tokens,
            }
        )
    outcome = getattr(event, "outcome", None)
    if isinstance(outcome, EventOutcome):
        span.set_attribute("my_code.outcome", outcome.value)
        if outcome in {EventOutcome.ERROR, EventOutcome.MISSING_OUTPUT}:
            span.set_status(Status(StatusCode.ERROR))


def _metric_dimensions(event: object) -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for key in ("provider", "model", "purpose", "tool_name", "outcome"):
        value = getattr(event, key, None)
        if isinstance(value, StrEnum):
            dimensions[f"my_code.{key}"] = value.value
        elif isinstance(value, str):
            dimensions[f"my_code.{key}"] = value
    return dimensions


def _timestamp_ns(value: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)


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


__all__ = ["OpenTelemetryBackend", "build_otel_backend"]
