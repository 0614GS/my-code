"""模型协议消息和流式事件值对象。"""

from dataclasses import dataclass

from nano_code.messages import (
    MessageRole,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

type ModelContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """仅包含模型协议所需字段，不携带 Transcript 本地元数据。"""

    role: MessageRole
    content: tuple[ModelContentBlock, ...]


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """模型流中仅用于展示的文本片段。"""

    text: str


@dataclass(frozen=True, slots=True)
class ModelResponseCompleted:
    """模型流中可安全校验并持久化的完整响应。"""

    response: ModelResponse


type ModelStreamEvent = ModelTextDelta | ModelResponseCompleted


__all__ = [
    "ModelContentBlock",
    "ModelMessage",
    "ModelResponseCompleted",
    "ModelStreamEvent",
    "ModelTextDelta",
]
