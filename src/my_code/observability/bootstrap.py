"""Build the optional OTLP observer."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from my_code.observability.api import NoOpTracer, OperationTracer
from my_code.observability.diagnostic_log import build_diagnostic_log
from my_code.observability.dispatcher import (
    ObservationDispatcher,
    ObservationSink,
)
from my_code.version import __version__

if TYPE_CHECKING:
    from my_code.observability.otel import OpenTelemetryBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ConfiguredBackend:
    backend: OpenTelemetryBackend
    traces_enabled: bool


def _build_otel_backend() -> _ConfiguredBackend | None:
    common = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    traces = common or bool(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
    metrics = common or bool(os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"))
    logs = common or bool(os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"))
    if not any((traces, metrics, logs)):
        return None
    try:
        from my_code.observability.otel import build_otel_backend

        backend = build_otel_backend(
            service_name=os.environ.get("OTEL_SERVICE_NAME", "my-code"),
            service_version=__version__,
            capture_content=_enabled("MY_CODE_OTEL_CAPTURE_CONTENT"),
            enable_traces=traces,
            enable_metrics=metrics,
            enable_logs=logs,
        )
        return _ConfiguredBackend(backend, traces)
    except Exception:
        logger.exception("OpenTelemetry initialization failed; disabling telemetry")
        return None


def build_observation_dispatcher(project_state_dir: Path) -> ObservationDispatcher:
    """按启动配置固定 tracer 与 sinks，运行期不允许动态改写订阅关系。"""

    configured = _build_otel_backend()
    backend = configured.backend if configured is not None else None
    tracer: OperationTracer = NoOpTracer()
    if configured is not None and configured.traces_enabled:
        tracer = configured.backend
    sinks: list[ObservationSink] = []
    diagnostics = build_diagnostic_log(project_state_dir)
    if diagnostics is not None:
        sinks.append(diagnostics)
    if backend is not None:
        sinks.append(backend)
    return ObservationDispatcher(
        tracer,
        sinks,
        capture_content=_enabled("MY_CODE_OTEL_CAPTURE_CONTENT"),
    )


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").casefold() in {"1", "true", "yes", "on"}


__all__ = ["build_observation_dispatcher"]
