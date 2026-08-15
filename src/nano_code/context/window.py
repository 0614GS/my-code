"""上下文消息预算的最终防线。"""

from nano_code.messages import (
    ChatMessage,
    SystemContextBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


class ContextWindow:
    """检测工作集溢出，但绝不静默丢弃历史。"""

    def __init__(self, max_chars: int = 160_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def project(self, messages: tuple[ChatMessage, ...]) -> tuple[ChatMessage, ...]:
        if not messages:
            return ()

        if not any(message.starts_context_segment for message in messages):
            raise ValueError("Conversation has no context segment boundary")
        current_chars = self.size(messages)
        if current_chars > self.max_chars:
            raise ContextOverflow(current_chars, self.max_chars)
        return messages

    @staticmethod
    def size(messages: tuple[ChatMessage, ...]) -> int:
        """返回工作集的保守字符估算。"""

        size = 0
        for message in messages:
            for block in message.content:
                if isinstance(block, TextBlock):
                    size += len(block.text)
                elif isinstance(block, SystemContextBlock):
                    size += len(block.content)
                elif isinstance(block, ToolUseBlock):
                    size += len(block.name) + len(str(block.input))
                elif isinstance(block, ToolResultBlock):
                    size += len(block.content)
        return size


class ContextOverflow(RuntimeError):
    """microcompact 后的工作集仍无法放入请求预算。"""

    def __init__(self, current_chars: int, max_chars: int) -> None:
        self.current_chars = current_chars
        self.max_chars = max_chars
        super().__init__(
            f"Context requires {current_chars} chars but the limit is {max_chars}"
        )
