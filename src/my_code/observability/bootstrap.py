"""Build the optional OTLP observer."""

from __future__ import annotations

import logging
import os

from my_code.observability.api import NoOpObserver, Observer

logger = logging.getLogger(__name__)


def build_observer() -> Observer:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    )
    if endpoint:
        try:
            from my_code.observability.otel import build_otel_observer

            return build_otel_observer(
                service_name=os.environ.get("OTEL_SERVICE_NAME", "my-code"),
                service_version="0.1.0",
                capture_content=_enabled("MY_CODE_OTEL_CAPTURE_CONTENT"),
            )
        except Exception:
            logger.exception("OpenTelemetry initialization failed; disabling telemetry")
    return NoOpObserver()


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").casefold() in {"1", "true", "yes", "on"}


__all__ = ["build_observer"]
