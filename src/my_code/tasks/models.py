"""Provider- and Agent-neutral supervised task lifecycle values."""

from dataclasses import dataclass
from enum import StrEnum


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


__all__ = [
    "TaskEvent",
    "TaskFailure",
    "TaskSnapshot",
    "TaskStatus",
]
