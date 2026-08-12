"""A minimal suffix projection that never cuts through a tool round."""

from nano_code.messages import ChatMessage, TextBlock, ToolResultBlock, ToolUseBlock


class ContextWindow:
    """Select recent complete human turns under an estimated character budget."""

    def __init__(self, max_chars: int = 160_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def project(self, messages: tuple[ChatMessage, ...]) -> tuple[ChatMessage, ...]:
        if not messages:
            return ()

        turn_starts = [
            index for index, message in enumerate(messages) if message.starts_human_turn
        ]
        if not turn_starts:
            raise ValueError("Conversation has no human turn boundary")

        selected_start = turn_starts[-1]
        selected_size = self._size(messages[selected_start:])
        for start in reversed(turn_starts[:-1]):
            candidate_size = self._size(messages[start:])
            if candidate_size > self.max_chars:
                break
            selected_start = start
            selected_size = candidate_size

        if selected_size > self.max_chars:
            raise ValueError(
                "The current user turn exceeds the MVP context budget; "
                "automatic summarization is not implemented yet"
            )
        selected = messages[selected_start:]
        self._validate_tool_pairs(selected)
        return selected

    @staticmethod
    def _size(messages: tuple[ChatMessage, ...]) -> int:
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
