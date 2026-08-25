"""Frontend-neutral events emitted by :class:`ChatService`."""

from dataclasses import dataclass

from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.todos.models import TodoItem
from my_code.model.primitives import ReasoningDisclosure, ReasoningPresentation
from my_code.tools.presentation import ToolUsePresentation


@dataclass(frozen=True, slots=True)
class TurnSucceeded:
    text: str
    completed_steps: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class MaxStepsReached:
    max_steps: int
    completed_steps: int
    input_tokens: int
    output_tokens: int


type TurnOutcome = TurnSucceeded | MaxStepsReached


@dataclass(frozen=True, slots=True)
class TextStarted:
    pass


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class TextCompleted:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningStarted:
    disclosure: ReasoningDisclosure


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    disclosure: ReasoningDisclosure
    part_index: int
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningCompleted:
    presentation: ReasoningPresentation


@dataclass(frozen=True, slots=True)
class AttachmentLoaded:
    path: str
    is_directory: bool
    display: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    tool_use_id: str
    presentation: ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolFinished:
    tool_use_id: str
    is_error: bool
    presentation: ToolResultPresentation


@dataclass(frozen=True, slots=True)
class TodoListUpdated:
    todos: tuple[TodoItem, ...]


type TurnEvent = (
    AttachmentLoaded
    | TextStarted
    | TextDelta
    | TextCompleted
    | ReasoningStarted
    | ReasoningDelta
    | ReasoningCompleted
    | ToolStarted
    | ToolFinished
    | TodoListUpdated
    | TurnSucceeded
    | MaxStepsReached
)


__all__ = [
    "AttachmentLoaded",
    "MaxStepsReached",
    "ReasoningCompleted",
    "ReasoningDelta",
    "ReasoningStarted",
    "TextCompleted",
    "TextDelta",
    "TextStarted",
    "TodoListUpdated",
    "ToolFinished",
    "ToolStarted",
    "TurnEvent",
    "TurnOutcome",
    "TurnSucceeded",
]
