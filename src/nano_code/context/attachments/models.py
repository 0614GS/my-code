"""不写 Transcript、只在模型上下文中交付的结构化 attachment。"""

from dataclasses import dataclass
from typing import Literal

from nano_code.context.documents import ContextInstruction
from nano_code.conversation import TextContent

type AttachmentRetention = Literal["request", "live_session"]


@dataclass(frozen=True, slots=True)
class ContextObservation:
    """Provider-neutral context observed by an attachment producer."""

    title: str
    body: str

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.body:
            raise ValueError("Context observation title and body must not be empty")


type AttachmentContent = TextContent | ContextInstruction | ContextObservation


@dataclass(frozen=True, slots=True)
class ContextAttachment:
    """一次请求或当前进程会话中的模型可见附加上下文。"""

    source: str
    content: tuple[AttachmentContent, ...]
    retention: AttachmentRetention = "request"

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.content:
            raise ValueError("Context attachment source and content must not be empty")
        if self.retention not in ("request", "live_session"):
            raise ValueError("Unsupported attachment retention")
        if not all(
            isinstance(block, (TextContent, ContextInstruction, ContextObservation))
            for block in self.content
        ):
            raise TypeError("Context attachment contains an unsupported content block")


__all__ = [
    "AttachmentContent",
    "AttachmentRetention",
    "ContextAttachment",
    "ContextObservation",
]
