"""不持久化、仅在一次模型请求中注入的上下文。"""

from dataclasses import dataclass, field
from typing import Literal

from nano_code.messages.conversation import TextContent

type ContextInstructionKind = Literal["system_reminder"]


@dataclass(frozen=True, slots=True)
class ContextInstruction:
    content: str
    kind: ContextInstructionKind = "system_reminder"
    type: Literal["context_instruction"] = field(
        default="context_instruction", init=False
    )

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Context instruction must not be empty")


type ContextContent = TextContent | ContextInstruction


@dataclass(frozen=True, slots=True)
class ContextAttachment:
    source: str
    content: tuple[ContextContent, ...]

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.content:
            raise ValueError("Context attachment source and content must not be empty")
        if not all(
            isinstance(block, (TextContent, ContextInstruction))
            for block in self.content
        ):
            raise TypeError("Context attachments contain only text or instructions")


@dataclass(frozen=True, slots=True)
class UserContextDocument:
    source: str
    content: tuple[ContextContent, ...]

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.content:
            raise ValueError("User context source and content must not be empty")
        if not all(
            isinstance(block, (TextContent, ContextInstruction))
            for block in self.content
        ):
            raise TypeError("User context contains only text or instructions")


__all__ = [
    "ContextAttachment",
    "ContextContent",
    "ContextInstruction",
    "ContextInstructionKind",
    "UserContextDocument",
]
