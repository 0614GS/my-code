"""Frontend-neutral projections used when a session is resumed."""

from dataclasses import dataclass
from typing import Literal

from my_code.chat.status import RuntimeStatus
from my_code.conversation.presentation import ToolResultPresentation
from my_code.model.primitives import ReasoningPresentation
from my_code.tools.presentation import ToolUsePresentation

type HistoryTextRole = Literal["user", "assistant", "system"]


@dataclass(frozen=True, slots=True)
class HistoryText:
    role: HistoryTextRole
    text: str
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class HistoryReasoning:
    presentation: ReasoningPresentation
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class HistoryToolCall:
    tool_use_id: str
    use: ToolUsePresentation
    result: ToolResultPresentation
    is_error: bool
    running: bool = False


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
