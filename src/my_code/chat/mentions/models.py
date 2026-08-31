"""Value objects owned by Chat's file mention capability."""

from dataclasses import dataclass

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


__all__ = [
    "FileMention",
    "LoadedAttachment",
    "PathSuggestion",
]
