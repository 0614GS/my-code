"""对旧工具结果生成稳定、可重放的轻量压缩决策。"""

from collections.abc import Callable
from dataclasses import replace

from my_code.context.attachments.projection import AttachmentProjector
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.state import ContentReplacement

_ELIGIBLE_TOOLS = frozenset({"Bash", "Glob", "Grep", "Read"})


class MicrocompactPolicy:
    """只回收可重新执行的旧只读/输出型工具结果。"""

    def __init__(
        self,
        *,
        trigger_chars: int,
        target_chars: int,
        min_result_chars: int = 2_000,
        keep_recent_batches: int = 2,
    ) -> None:
        if target_chars < 1 or trigger_chars < target_chars:
            raise ValueError("Microcompact thresholds are invalid")
        if min_result_chars < 1 or keep_recent_batches < 0:
            raise ValueError("Microcompact limits are invalid")
        self.trigger_chars = trigger_chars
        self.target_chars = target_chars
        self.min_result_chars = min_result_chars
        self.keep_recent_batches = keep_recent_batches

    @classmethod
    def for_window(cls, max_chars: int) -> "MicrocompactPolicy":
        return cls(
            trigger_chars=max_chars,
            target_chars=max(1, max_chars * 3 // 4),
        )

    def propose(
        self,
        messages: tuple[ConversationEntry, ...],
        existing: tuple[ContentReplacement, ...],
        *,
        additional_chars: int = 0,
    ) -> tuple[ContentReplacement, ...]:
        """按消息顺序返回达到目标预算所需的新决策。"""

        replacements = {item.tool_use_id: item for item in existing}
        if additional_chars < 0:
            raise ValueError("additional_chars must not be negative")
        current_chars = (
            _effective_message_chars(messages, replacements) + additional_chars
        )
        if current_chars <= self.trigger_chars:
            return ()

        tool_names = {
            block.id: block.name
            for message in messages
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolCall)
        }
        candidates = self._candidates(messages, replacements, tool_names)

        proposed: list[ContentReplacement] = []
        for block in candidates:
            tool_name = tool_names[block.tool_use_id]
            replacement = ContentReplacement.for_tool_result(
                tool_use_id=block.tool_use_id,
                tool_name=tool_name,
                original_chars=len(block.content),
            )
            proposed.append(replacement)
            current_chars -= len(block.content) - len(replacement.content)
            if current_chars <= self.target_chars:
                break
        return tuple(proposed)

    def propose_tokens(
        self,
        messages: tuple[ConversationEntry, ...],
        existing: tuple[ContentReplacement, ...],
        *,
        current_tokens: int,
        trigger_tokens: int,
        estimate: Callable[[tuple[ConversationEntry, ...]], int],
    ) -> tuple[ContentReplacement, ...]:
        """Replace oldest eligible results until the retokenized request is safe."""

        if current_tokens < trigger_tokens:
            return ()
        target_tokens = max(1, trigger_tokens * 9 // 10)
        replacements = {item.tool_use_id: item for item in existing}
        tool_names = {
            block.id: block.name
            for message in messages
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolCall)
        }
        candidates = self._candidates(messages, replacements, tool_names)
        proposed: list[ContentReplacement] = []
        for block in candidates:
            replacement = ContentReplacement.for_tool_result(
                tool_use_id=block.tool_use_id,
                tool_name=tool_names[block.tool_use_id],
                original_chars=len(block.content),
            )
            proposed.append(replacement)
            view = apply_content_replacements(messages, existing + tuple(proposed))
            if estimate(view) <= target_tokens:
                break
        return tuple(proposed)

    def _candidates(
        self,
        messages: tuple[ConversationEntry, ...],
        replacements: dict[str, ContentReplacement],
        tool_names: dict[str, str],
    ) -> list[ToolResult]:
        batches = [
            message for message in messages if isinstance(message, ToolResultBatch)
        ]
        eligible_batches = (
            batches[: -self.keep_recent_batches]
            if self.keep_recent_batches
            else batches
        )
        return [
            block
            for message in eligible_batches
            for block in message.content
            if block.tool_use_id not in replacements
            and tool_names.get(block.tool_use_id) in _ELIGIBLE_TOOLS
            and len(block.content) >= self.min_result_chars
        ]


def apply_content_replacements(
    messages: tuple[ConversationEntry, ...],
    replacements: tuple[ContentReplacement, ...],
) -> tuple[ConversationEntry, ...]:
    """创建模型工作视图，不修改 Transcript 中的原始消息。"""

    by_id = {item.tool_use_id: item for item in replacements}
    updated: list[ConversationEntry] = []
    for message in messages:
        if not isinstance(message, ToolResultBatch):
            updated.append(message)
            continue
        content = tuple(
            replace(block, content=by_id[block.tool_use_id].content)
            if isinstance(block, ToolResult) and block.tool_use_id in by_id
            else block
            for block in message.content
        )
        updated.append(replace(message, content=content))
    return tuple(updated)


def _effective_message_chars(
    messages: tuple[ConversationEntry, ...],
    replacements: dict[str, ContentReplacement],
) -> int:
    size = 0
    for message in messages:
        if isinstance(message, AttachmentMessage):
            size += AttachmentProjector().measure((message.payload,))
            continue
        if isinstance(message, (HumanMessage, ConversationSummaryMessage)):
            size += len(message.content)
            continue
        for block in message.content:
            if isinstance(block, ToolResult):
                size += (
                    len(replacements[block.tool_use_id].content)
                    if block.tool_use_id in replacements
                    else len(block.content)
                )
            elif isinstance(block, ToolCall):
                size += len(block.name) + len(str(block.input))
            else:
                if isinstance(block, TextContent):
                    size += len(block.text)
                else:
                    assert isinstance(block, ReasoningContent)
    return size
