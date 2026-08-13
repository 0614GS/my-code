"""单次模型响应流式传输期间发出的 provider 无关事件。"""

from dataclasses import dataclass

from nano_code.messages import ModelResponse


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """provider 流中一个仅用于展示的文本片段。"""

    text: str


@dataclass(frozen=True, slots=True)
class ModelResponseCompleted:
    """可安全校验并持久化的完整响应。"""

    response: ModelResponse


type ModelStreamEvent = ModelTextDelta | ModelResponseCompleted
