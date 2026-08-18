"""不持久化的用户上下文文档与可信指令块。"""

from dataclasses import dataclass, field
from typing import Literal

from nano_code.conversation import TextContent

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


type ContextDocumentContent = TextContent | ContextInstruction


@dataclass(frozen=True, slots=True)
class UserContextDocument:
    source: str
    content: tuple[ContextDocumentContent, ...]

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.content:
            raise ValueError("User context source and content must not be empty")
        if not all(
            isinstance(block, (TextContent, ContextInstruction))
            for block in self.content
        ):
            raise TypeError("User context contains only text or instructions")


__all__ = [
    "ContextDocumentContent",
    "ContextInstruction",
    "ContextInstructionKind",
    "UserContextDocument",
]
