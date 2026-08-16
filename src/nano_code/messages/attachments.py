"""Non-persistent request attachments supplied after transcript messages."""

from dataclasses import dataclass

from nano_code.messages.user_context import ContextContentBlock


@dataclass(frozen=True, slots=True)
class AttachmentMessage:
    """A named, request-scoped message outside the Transcript."""

    source: str
    content: tuple[ContextContentBlock, ...]

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("Attachment source must not be empty")
        if not self.content:
            raise ValueError("Attachment message must contain at least one block")


__all__ = ["AttachmentMessage"]
