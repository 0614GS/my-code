"""不会截断工具轮次的最小后缀投影。"""

from nano_code.messages import ChatMessage, TextBlock, ToolResultBlock, ToolUseBlock


class ContextWindow:
    """在估算字符预算内选择最近的完整用户轮次。"""

    def __init__(self, max_chars: int = 160_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def project(self, messages: tuple[ChatMessage, ...]) -> tuple[ChatMessage, ...]:
        if not messages:
            return ()

        # Claude Code 的 compact 流程能总结和修复异常轮次，因此可以在更细的
        # API 轮次边界截断。在 nano-code 具备该机制前，真实用户提示是保守的安全边界。
        turn_starts = [
            index for index, message in enumerate(messages) if message.starts_human_turn
        ]
        if not turn_starts:
            raise ValueError("Conversation has no human turn boundary")

        # 当前用户轮次必须保留。仅在更早的完整轮次仍能容纳时向前扩展；
        # 起点越早，估算值只会增大。
        selected_start = turn_starts[-1]
        selected_size = self._size(messages[selected_start:])
        for start in reversed(turn_starts[:-1]):
            candidate_size = self._size(messages[start:])
            if candidate_size > self.max_chars:
                break
            selected_start = start
            selected_size = candidate_size

        if selected_size > self.max_chars:
            # 绝不为了满足预算而切割当前轮次，否则可能产生孤立工具结果，
            # 或移除赋予该轮次语义的指令。
            raise ValueError(
                "The current user turn exceeds the MVP context budget; "
                "automatic summarization is not implemented yet"
            )
        selected = messages[selected_start:]

        # 将配对校验作为 API 调用前最后一道协议防火墙。
        self._validate_tool_pairs(selected)
        return selected

    @staticmethod
    def _size(messages: tuple[ChatMessage, ...]) -> int:
        # 字符数刻意只作为 MVP 阶段的估算。未来由 token 感知的 compact 替代，
        # 但不会改变投影边界契约。
        size = 0
        for message in messages:
            for block in message.content:
                if isinstance(block, TextBlock):
                    size += len(block.text)
                elif isinstance(block, ToolUseBlock):
                    size += len(block.name) + len(str(block.input))
                elif isinstance(block, ToolResultBlock):
                    size += len(block.content)
        return size

    @staticmethod
    def _validate_tool_pairs(messages: tuple[ChatMessage, ...]) -> None:
        pending: set[str] = set()
        for message in messages:
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    pending.add(block.id)
                elif isinstance(block, ToolResultBlock):
                    if block.tool_use_id not in pending:
                        raise ValueError(
                            f"Orphan tool result in context: {block.tool_use_id}"
                        )
                    pending.remove(block.tool_use_id)
        if pending:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(f"Unresolved tool use in context: {unresolved}")
