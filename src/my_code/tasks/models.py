"""Provider- and Agent-neutral supervised task lifecycle values."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class TaskFailure:
    kind: str
    message: str

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.message.strip():
            raise ValueError("Task failure kind and message must not be blank")


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    name: str
    parent_task_id: str | None
    status: TaskStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: object | None = None
    failure: TaskFailure | None = None


@dataclass(frozen=True, slots=True)
class TaskEvent:
    sequence: int
    snapshot: TaskSnapshot


@dataclass(frozen=True, slots=True)
class SubagentTranscriptText:
    role: str
    text: str
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class SubagentTranscriptReasoning:
    disclosure: Literal["verbatim", "summary", "redacted", "hidden"]
    parts: tuple[str, ...]
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class SubagentToolUseView:
    display_name: str
    summary: str
    activity: str


@dataclass(frozen=True, slots=True)
class SubagentToolResultView:
    summary: str
    detail: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SubagentTranscriptTool:
    tool_use_id: str
    use: SubagentToolUseView
    result: SubagentToolResultView | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class SubagentTaskView:
    task_id: str
    run_id: str
    agent_type: str
    description: str
    background: bool
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    input_tokens: int
    output_tokens: int
    transcript: tuple[
        SubagentTranscriptText | SubagentTranscriptReasoning | SubagentTranscriptTool,
        ...,
    ]
    active_tool_ids: tuple[str, ...]
    error: str | None = None


__all__ = [
    "TaskEvent",
    "TaskFailure",
    "TaskSnapshot",
    "TaskStatus",
    "SubagentTranscriptReasoning",
    "SubagentTranscriptText",
    "SubagentTranscriptTool",
    "SubagentToolResultView",
    "SubagentToolUseView",
    "SubagentTaskView",
]
