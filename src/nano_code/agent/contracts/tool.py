"""模型可见工具定义和一次工具轮次的事件值对象。"""

from dataclasses import dataclass

from nano_code.messages import ChatMessage, JsonObject, ToolResultBlock, ToolUseBlock
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """暴露给模型的稳定工具标识和 schema。"""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """一个工具调用开始执行。"""

    call: ToolUseBlock
    presentation: ToolUsePresentation

    @property
    def tool_use_id(self) -> str:
        return self.call.id

    @property
    def name(self) -> str:
        return self.call.name

    @property
    def input(self) -> JsonObject:
        return self.call.input


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """一个工具调用完成，并携带要发给模型的结果。"""

    call: ToolUseBlock
    result: ToolResultBlock
    presentation: ToolResultPresentation

    @property
    def tool_use_id(self) -> str:
        return self.call.id

    @property
    def name(self) -> str:
        return self.call.name

    @property
    def input(self) -> JsonObject:
        return self.call.input

    @property
    def is_error(self) -> bool:
        return self.result.is_error


@dataclass(frozen=True, slots=True)
class ToolRoundCompleted:
    """工具结果消息已经组装，供 Agent 追加到会话。"""

    message: ChatMessage
    results: tuple[ToolResultBlock, ...]
    cancelled: bool = False

    @property
    def result_message(self) -> ChatMessage:
        return self.message

    @property
    def tool_results(self) -> tuple[ToolResultBlock, ...]:
        return self.results


type ToolRoundEvent = ToolCallStarted | ToolCallFinished | ToolRoundCompleted

__all__ = [
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolDefinition",
    "ToolRoundCompleted",
    "ToolRoundEvent",
]
