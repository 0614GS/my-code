"""Frontend-neutral projections used when a session is resumed."""

from dataclasses import dataclass
from typing import Literal

from nano_code.chat.status import RuntimeStatus
from nano_code.model.primitives import ReasoningPresentation
from nano_code.tools.presentation import ToolResultPresentation, ToolUsePresentation

type HistoryTextRole = Literal["user", "assistant", "system"]


@dataclass(frozen=True, slots=True)
class HistoryText:
    role: HistoryTextRole
    text: str


@dataclass(frozen=True, slots=True)
class HistoryReasoning:
    presentation: ReasoningPresentation


@dataclass(frozen=True, slots=True)
class HistoryToolCall:
    tool_use_id: str
    use: ToolUsePresentation
    result: ToolResultPresentation
    is_error: bool


type HistoryEntry = HistoryText | HistoryReasoning | HistoryToolCall


@dataclass(frozen=True, slots=True)
class ResumedSession:
    status: RuntimeStatus
    history: tuple[HistoryEntry, ...]


__all__ = [
    "HistoryEntry",
    "HistoryReasoning",
    "HistoryText",
    "HistoryTextRole",
    "HistoryToolCall",
    "ResumedSession",
]
