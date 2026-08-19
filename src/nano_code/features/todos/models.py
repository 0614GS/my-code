"""Strongly typed Todo domain values."""

from dataclasses import dataclass
from typing import Literal

type TodoStatus = Literal["pending", "in_progress", "completed"]

TODO_STATUSES = frozenset(("pending", "in_progress", "completed"))


@dataclass(frozen=True, slots=True)
class TodoItem:
    """A model-managed task and its present-continuous display label."""

    content: str
    status: TodoStatus
    active_form: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Todo content must not be empty")
        if self.status not in TODO_STATUSES:
            raise ValueError(f"Unsupported todo status: {self.status}")
        if not self.active_form.strip():
            raise ValueError("Todo activeForm must not be empty")


__all__ = [
    "TODO_STATUSES",
    "TodoItem",
    "TodoStatus",
]
