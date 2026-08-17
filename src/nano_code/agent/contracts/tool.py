"""一次 ToolRound 的事件值对象。"""

from dataclasses import dataclass

from nano_code.messages import (
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """一个工具调用开始执行。"""

    call: ToolCall
    presentation: ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """一个工具调用完成，并携带要发给模型的结果。"""

    call: ToolCall
    result: ToolResult
    presentation: ToolResultPresentation


@dataclass(frozen=True, slots=True)
class ToolRoundCompleted:
    """工具结果消息已经组装，供 Agent 追加到会话。"""

    message: ToolResultsMessage
    cancelled: bool = False


type ToolRoundEvent = ToolCallStarted | ToolCallFinished | ToolRoundCompleted

__all__ = [
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolRoundCompleted",
    "ToolRoundEvent",
]
