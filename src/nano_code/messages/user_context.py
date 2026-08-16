"""Non-persistent user context supplied before transcript messages."""

from dataclasses import dataclass

from nano_code.messages.models import SystemContextBlock, TextBlock

type ContextContentBlock = TextBlock | SystemContextBlock


@dataclass(frozen=True, slots=True)
class UserContextMessage:
    """A named, non-Transcript user-context message."""

    source: str
    content: tuple[ContextContentBlock, ...]

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("User context source must not be empty")
        if not self.content:
            raise ValueError("User context message must contain at least one block")


__all__ = ["ContextContentBlock", "UserContextMessage"]
