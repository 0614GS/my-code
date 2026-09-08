"""可观测性事件契约；只表达稳定元数据，不承担业务状态持久化。"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import ClassVar, Protocol

from my_code.foundation.json import JsonValue


class EventOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    DENIED = "denied"
    LIMIT = "limit"
    MISSING_OUTPUT = "missing_output"


@dataclass(frozen=True, slots=True)
class SensitiveContent:
    """只有显式允许内容采集的 sink 才能读取的正文。"""

    value: JsonValue


class ObservationEvent(Protocol):
    name: ClassVar[str]


@dataclass(frozen=True, slots=True)
class InvocationStarted:
    name: ClassVar[str] = "invocation.started"
    continuation: bool


@dataclass(frozen=True, slots=True)
class InvocationFinished:
    name: ClassVar[str] = "invocation.finished"
    outcome: EventOutcome
    duration_ms: float
    completed_steps: int | None = None
    max_steps: int | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRequestStarted:
    name: ClassVar[str] = "model.request.started"
    purpose: str
    provider: str
    model: str
    request_id: str | None = None
    step: int | None = None
    attempt: int | None = None
    content: SensitiveContent | None = None


@dataclass(frozen=True, slots=True)
class ModelResponseReceived:
    name: ClassVar[str] = "model.response.received"
    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    provider_reported: bool
    stop_reason: str
    request_id: str | None = None
    step: int | None = None
    attempt: int | None = None
    first_event_ms: float | None = None
    first_text_ms: float | None = None
    content: SensitiveContent | None = None


@dataclass(frozen=True, slots=True)
class ModelRequestFinished:
    name: ClassVar[str] = "model.request.finished"
    purpose: str
    provider: str
    model: str
    outcome: EventOutcome
    duration_ms: float
    request_id: str | None = None
    step: int | None = None
    attempt: int | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionStarted:
    name: ClassVar[str] = "tool.execution.started"
    tool_name: str
    tool_call_id: str
    input_sha256: str


@dataclass(frozen=True, slots=True)
class ToolExecutionFinished:
    name: ClassVar[str] = "tool.execution.finished"
    tool_name: str
    tool_call_id: str
    outcome: EventOutcome
    duration_ms: float
    is_error: bool
    input_sha256: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ToolPermissionEvaluated:
    name: ClassVar[str] = "tool.permission.evaluated"
    tool_name: str
    tool_call_id: str
    origin: str
    behavior: str
    reason_kind: str
    authority: str
    execution_backend: str


@dataclass(frozen=True, slots=True)
class JournalWriteFailed:
    name: ClassVar[str] = "journal.write_failed"
    record_type: str
    error_type: str


type RuntimeObservationEvent = (
    InvocationStarted
    | InvocationFinished
    | ModelRequestStarted
    | ModelResponseReceived
    | ModelRequestFinished
    | ToolExecutionStarted
    | ToolExecutionFinished
    | ToolPermissionEvaluated
    | JournalWriteFailed
)


def event_attributes(event: ObservationEvent) -> dict[str, bool | int | float | str]:
    """投影 sink 可共享的低基数或关联元数据，正文必须走独立字段。"""

    attributes: dict[str, bool | int | float | str] = {}
    for item in fields(event):  # type: ignore[arg-type]
        if item.name == "content":
            continue
        value = getattr(event, item.name)
        if isinstance(value, StrEnum):
            attributes[item.name] = value.value
        elif isinstance(value, (bool, int, float, str)):
            attributes[item.name] = value
    return attributes


def sensitive_content(event: ObservationEvent) -> SensitiveContent | None:
    value = getattr(event, "content", None)
    return value if isinstance(value, SensitiveContent) else None


__all__ = [
    "EventOutcome",
    "InvocationFinished",
    "InvocationStarted",
    "JournalWriteFailed",
    "ModelRequestFinished",
    "ModelRequestStarted",
    "ModelResponseReceived",
    "ObservationEvent",
    "RuntimeObservationEvent",
    "SensitiveContent",
    "ToolExecutionFinished",
    "ToolExecutionStarted",
    "ToolPermissionEvaluated",
    "event_attributes",
    "sensitive_content",
]
