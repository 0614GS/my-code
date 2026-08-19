"""工具契约、注册与执行。"""

from nano_code.tools.base import Tool, ToolContext
from nano_code.tools.presentation import (
    ToolResultPresentation,
    ToolUsePresentation,
    compact_text,
    generic_tool_use_presentation,
)
from nano_code.tools.registry import ToolRegistry
from nano_code.tools.round_executor import (
    ToolCallFinished,
    ToolCallStarted,
    ToolRoundCompleted,
    ToolRoundEvent,
    ToolRoundExecutor,
)

__all__ = [
    "Tool",
    "ToolContext",
    "ToolResultPresentation",
    "ToolUsePresentation",
    "ToolRegistry",
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolRoundCompleted",
    "ToolRoundEvent",
    "ToolRoundExecutor",
    "compact_text",
    "generic_tool_use_presentation",
]
