"""Context working-set sizing used by microcompaction diagnostics."""

from my_code.context.attachments.projection import AttachmentProjector
from my_code.conversation.models import (
    AttachmentMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
)


class ContextWindow:
    """Measure the working set without creating a second model limit.

    ``max_chars`` is a heuristic target for content replacement.  Full
    compaction is governed only by the active model's token budget; a separate
    character ceiling can otherwise compact a multilingual or tool-heavy
    session while the displayed token window still has ample room.
    """

    def __init__(self, max_chars: int = 160_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def ensure_fits(
        self,
        messages: tuple[ConversationEntry, ...],
        *,
        additional_chars: int = 0,
    ) -> tuple[ConversationEntry, ...]:
        if additional_chars < 0:
            raise ValueError("additional_chars must not be negative")
        if not messages:
            return ()

        if not any(message.starts_context_segment for message in messages):
            raise ValueError("Conversation has no context segment boundary")
        # ``additional_chars`` remains part of this compatibility surface for
        # callers that size opaque continuation data. The request tokenizer
        # accounts for that payload when it makes the capacity decision.
        return messages

    @staticmethod
    def size(messages: tuple[ConversationEntry, ...]) -> int:
        """返回工作集的保守字符估算。"""

        size = 0
        for message in messages:
            if isinstance(message, AttachmentMessage):
                size += AttachmentProjector().measure((message.payload,))
                continue
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
                elif isinstance(block, ReasoningContent):
                    # Completed provider-private reasoning is not ordinary context.
                    # The normalized active trajectory is budgeted by ContextPlanner.
                    continue
        return size


__all__ = [
    "ContextWindow",
]
