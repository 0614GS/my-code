"""对旧工具结果生成稳定、可重放的轻量压缩决策。"""

from dataclasses import replace

from nano_code.context.models import ContentReplacement
from nano_code.messages import ChatMessage, ToolResultBlock, ToolUseBlock

_ELIGIBLE_TOOLS = frozenset({"Bash", "Glob", "Grep", "Read"})


class MicrocompactPolicy:
    """只回收可重新执行的旧只读/输出型工具结果。"""

    def __init__(
        self,
        *,
        trigger_chars: int,
        target_chars: int,
        min_result_chars: int = 2_000,
        keep_recent_results: int = 1,
    ) -> None:
        if target_chars < 1 or trigger_chars < target_chars:
            raise ValueError("Microcompact thresholds are invalid")
        if min_result_chars < 1 or keep_recent_results < 0:
            raise ValueError("Microcompact limits are invalid")
        self.trigger_chars = trigger_chars
        self.target_chars = target_chars
        self.min_result_chars = min_result_chars
        self.keep_recent_results = keep_recent_results

    @classmethod
    def for_window(cls, max_chars: int) -> "MicrocompactPolicy":
        return cls(
            trigger_chars=max_chars,
            target_chars=max(1, max_chars * 3 // 4),
        )

    def propose(
        self,
        messages: tuple[ChatMessage, ...],
        existing: tuple[ContentReplacement, ...],
    ) -> tuple[ContentReplacement, ...]:
        """按消息顺序返回达到目标预算所需的新决策。"""

        replacements = {item.tool_use_id: item for item in existing}
        current_chars = _effective_message_chars(messages, replacements)
        if current_chars <= self.trigger_chars:
            return ()

        tool_names = {
            block.id: block.name
            for message in messages
            for block in message.content
            if isinstance(block, ToolUseBlock)
        }
        candidates = [
            block
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
            and block.tool_use_id not in replacements
            and tool_names.get(block.tool_use_id) in _ELIGIBLE_TOOLS
            and len(block.content) >= self.min_result_chars
        ]
        if self.keep_recent_results:
            candidates = candidates[: -self.keep_recent_results]

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


def apply_content_replacements(
    messages: tuple[ChatMessage, ...],
    replacements: tuple[ContentReplacement, ...],
) -> tuple[ChatMessage, ...]:
    """创建模型工作视图，不修改 Transcript 中的原始消息。"""

    by_id = {item.tool_use_id: item for item in replacements}
    projected: list[ChatMessage] = []
    for message in messages:
        content = tuple(
            replace(block, content=by_id[block.tool_use_id].content)
            if isinstance(block, ToolResultBlock) and block.tool_use_id in by_id
            else block
            for block in message.content
        )
        projected.append(replace(message, content=content))
    return tuple(projected)


def _effective_message_chars(
    messages: tuple[ChatMessage, ...],
    replacements: dict[str, ContentReplacement],
) -> int:
    size = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                size += (
                    len(replacements[block.tool_use_id].content)
                    if block.tool_use_id in replacements
                    else len(block.content)
                )
            elif isinstance(block, ToolUseBlock):
                size += len(block.name) + len(str(block.input))
            else:
                size += len(block.text)
    return size
