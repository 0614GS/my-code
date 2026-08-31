"""Frontend-neutral input and file mention values."""

from dataclasses import dataclass
from enum import StrEnum

from my_code.conversation.attachments import FileMentionAttachment


@dataclass(frozen=True, slots=True)
class FileMention:
    """One syntactically valid file mention in its original prompt."""

    path: str
    raw: str
    start: int
    end: int
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True, slots=True)
class LoadedAttachment:
    """A successfully loaded mention and its model-visible attachment."""

    attachment: FileMentionAttachment
    path: str
    is_directory: bool

    @property
    def display(self) -> str:
        action = "Listed directory" if self.is_directory else "Read"
        return f"{action} {self.path}"


@dataclass(frozen=True, slots=True)
class PathSuggestion:
    """One frontend-neutral workspace path completion."""

    path: str
    is_directory: bool
    display: str


class QueueInputState(StrEnum):
    PREPARING = "preparing"
    QUEUED = "queued"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QueuedInputView:
    input_id: str
    prompt: str
    state: QueueInputState
    error: str | None = None


__all__ = [
    "FileMention",
    "LoadedAttachment",
    "PathSuggestion",
    "QueuedInputView",
    "QueueInputState",
]
