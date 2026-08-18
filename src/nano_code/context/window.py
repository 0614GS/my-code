"""上下文消息预算的最终防线。"""

from nano_code.agent.errors import ContextOverflow as _ContextOverflow
from nano_code.conversation import (
    ConversationMessage,
    ConversationSummaryMessage,
    HumanMessage,
    OpaqueAssistantContent,
    TextContent,
    ToolCall,
    ToolResult,
)


class ContextWindow:
    """检测工作集溢出，但绝不静默丢弃历史。"""

    def __init__(self, max_chars: int = 160_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def ensure_fits(
        self,
        messages: tuple[ConversationMessage, ...],
        *,
        additional_chars: int = 0,
    ) -> tuple[ConversationMessage, ...]:
        if additional_chars < 0:
            raise ValueError("additional_chars must not be negative")
        if not messages:
            if additional_chars > self.max_chars:
                raise _ContextOverflow(additional_chars, self.max_chars)
            return ()

        if not any(message.starts_context_segment for message in messages):
            raise ValueError("Conversation has no context segment boundary")
        current_chars = self.size(messages) + additional_chars
        if current_chars > self.max_chars:
            raise _ContextOverflow(current_chars, self.max_chars)
        return messages

    @staticmethod
    def size(messages: tuple[ConversationMessage, ...]) -> int:
        """返回工作集的保守字符估算。"""

        size = 0
        for message in messages:
            if isinstance(message, (HumanMessage, ConversationSummaryMessage)):
                size += len(message.content)
                continue
            for block in message.content:
                if isinstance(block, TextContent):
                    size += len(block.text)
                elif isinstance(block, ToolCall):
                    size += len(block.name) + len(str(block.input))
                elif isinstance(block, ToolResult):
                    size += len(block.content)
                elif isinstance(block, OpaqueAssistantContent):
                    # Completed provider-private reasoning is not ordinary context.
                    # The normalized active trajectory is budgeted by ContextPlanner.
                    continue
        return size


__all__ = ["ContextWindow"]
