"""OTel signal 配置与 dispatcher 装配。"""

from typing import cast

from my_code.observability import bootstrap
from my_code.observability.otel import OpenTelemetryBackend

_ENDPOINTS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
)


def test_no_endpoint_does_not_build_otel(monkeypatch) -> None:
    for name in _ENDPOINTS:
        monkeypatch.delenv(name, raising=False)

    assert bootstrap._build_otel_backend() is None


def test_signal_specific_endpoint_only_enables_that_exporter(monkeypatch) -> None:
    for name in _ENDPOINTS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://collector/logs")
    captured: dict[str, object] = {}
    sentinel = cast(OpenTelemetryBackend, object())

    def build(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("my_code.observability.otel.build_otel_backend", build)

    configured = bootstrap._build_otel_backend()

    assert configured is not None
    assert configured.backend is sentinel
    assert configured.traces_enabled is False
    assert captured["enable_logs"] is True
    assert captured["enable_traces"] is False
    assert captured["enable_metrics"] is False
