"""不写 Transcript、只在模型上下文中交付的结构化 attachment。"""

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from nano_code.messages.conversation import TextContent
from nano_code.messages.primitives import JsonObject

from .context import ContextInstruction

type AttachmentRetention = Literal["request", "live_session"]


@dataclass(frozen=True, slots=True)
class AttachmentToolExchange:
    """由可信内部 source 合成的一对 tool_use/tool_result。"""

    tool_name: str
    tool_input: JsonObject
    result_content: str
    is_error: bool = False
    tool_use_id: str = field(default_factory=lambda: f"attachment-{uuid4()}")

    def __post_init__(self) -> None:
        if not self.tool_name.strip() or not self.tool_use_id.strip():
            raise ValueError("Attachment tool name and use ID must not be empty")
        if not self.result_content:
            raise ValueError("Attachment tool result must not be empty")


type AttachmentContent = TextContent | ContextInstruction | AttachmentToolExchange


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
            isinstance(block, (TextContent, ContextInstruction, AttachmentToolExchange))
            for block in self.content
        ):
            raise TypeError("Context attachment contains an unsupported content block")


__all__ = [
    "AttachmentContent",
    "AttachmentRetention",
    "AttachmentToolExchange",
    "ContextAttachment",
]
