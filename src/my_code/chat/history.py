"""Frontend-neutral projections used when a session is resumed."""

from dataclasses import dataclass
from typing import Literal

from my_code.chat.status import RuntimeStatus
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.todos.models import TodoItem
from my_code.foundation.json import JsonObject
from my_code.model.primitives import ReasoningPresentation
from my_code.tools.presentation import ToolUsePresentation

type HistoryTextRole = Literal["user", "assistant", "system"]


@dataclass(frozen=True, slots=True)
class HistoryText:
    role: HistoryTextRole
    text: str
    streaming: bool = False
    is_final_answer: bool = False


@dataclass(frozen=True, slots=True)
class HistoryReasoning:
    presentation: ReasoningPresentation
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class HistoryPlan:
    plan: str


@dataclass(frozen=True, slots=True)
class HistoryContextItem:
    source: str
    attachment_kind: str | None
    text: str


@dataclass(frozen=True, slots=True)
class HistoryContextGroup:
    request_number: int
    items: tuple[HistoryContextItem, ...]


@dataclass(frozen=True, slots=True)
class HistoryToolCall:
    tool_use_id: str
    use: ToolUsePresentation
    result: ToolResultPresentation
    is_error: bool
    running: bool = False
    todos: tuple[TodoItem, ...] | None = None
    ends_tool_batch: bool = False
    name: str | None = None
    input: JsonObject | None = None


type HistoryEntry = (
    HistoryText | HistoryReasoning | HistoryPlan | HistoryContextGroup | HistoryToolCall
)


@dataclass(frozen=True, slots=True)
class ResumedSession:
    status: RuntimeStatus
    history: tuple[HistoryEntry, ...]


__all__ = [
    "HistoryEntry",
    "HistoryContextGroup",
    "HistoryContextItem",
    "HistoryPlan",
    "HistoryReasoning",
    "HistoryText",
    "HistoryTextRole",
    "HistoryToolCall",
    "ResumedSession",
]
