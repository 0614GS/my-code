"""Concrete persisted Session built around a pure Conversation aggregate."""

from collections.abc import Iterable

from nano_code.conversation.models import (
    AssistantMessage,
    ConversationMessage,
    ConversationSummaryMessage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.conversation.state import (
    CompactBoundary,
    ContentReplacement,
    Conversation,
)
from nano_code.sessions.store import SessionStore
from nano_code.tools.presentation import ToolResultPresentation


class Session:
    """Own one Conversation and commit recoverable changes persistence-first."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        loaded = store.load()
        self.conversation = Conversation(
            loaded.history,
            content_replacements=loaded.content_replacements,
            compact_boundaries=loaded.compact_boundaries,
        )
        self._tool_presentations = dict(loaded.tool_presentations)
        self._repair_trailing_tool_calls()

    @classmethod
    def restore(cls, store: SessionStore) -> "Session":
        candidate = cls(store)
        if not candidate.history:
            raise ValueError(f"Session contains no messages: {store.session_id}")
        return candidate

    @property
    def session_id(self) -> str:
        return self.store.session_id

    @property
    def history(self) -> tuple[ConversationMessage, ...]:
        return self.conversation.history

    @property
    def working_messages(self) -> tuple[ConversationMessage, ...]:
        return self.conversation.working_set

    @property
    def content_replacements(self) -> tuple[ContentReplacement, ...]:
        return self.conversation.content_replacements

    @property
    def compact_boundaries(self) -> tuple[CompactBoundary, ...]:
        return self.conversation.compact_boundaries

    @property
    def message_count(self) -> int:
        return len(self.working_messages)

    @property
    def history_message_count(self) -> int:
        return len(self.history)

    @property
    def content_replacement_count(self) -> int:
        return len(self.content_replacements)

    @property
    def compact_count(self) -> int:
        return len(self.compact_boundaries)

    def append(
        self,
        message: ConversationMessage,
        *,
        presentations: Iterable[tuple[str, ToolResultPresentation]] = (),
    ) -> None:
        candidate = self.conversation.clone()
        changed = candidate.append(message)
        if not changed:
            return
        presentation_items = tuple(presentations)
        if not self.store.append_message(message, presentation_items):
            raise ValueError(
                f"Message UUID already exists outside the active conversation: "
                f"{message.uuid}"
            )
        self.conversation = candidate
        self._tool_presentations.update(presentation_items)

    def append_tool_results(
        self,
        results: Iterable[ToolResult],
        assistant_message: AssistantMessage,
        *,
        presentations: Iterable[tuple[str, ToolResultPresentation]] = (),
    ) -> ToolResultsMessage:
        result_blocks = tuple(results)
        if not result_blocks:
            raise ValueError("A tool result message must contain at least one result")
        message = ToolResultsMessage(
            content=result_blocks,
            parent_uuid=assistant_message.uuid,
            source_assistant_uuid=assistant_message.uuid,
        )
        self.append(message, presentations=presentations)
        return message

    def append_content_replacement(self, replacement: ContentReplacement) -> None:
        candidate = self.conversation.clone()
        if not candidate.add_content_replacement(replacement):
            return
        self.store.append_content_replacement(replacement)
        self.conversation = candidate

    def commit_compaction(
        self,
        replacements: tuple[ContentReplacement, ...],
        summary: ConversationSummaryMessage,
        boundary: CompactBoundary,
    ) -> CompactBoundary:
        candidate = self.conversation.clone()
        for replacement in replacements:
            candidate.add_content_replacement(replacement)
        candidate.add_compact_boundary(boundary)
        candidate.append(summary)
        self.store.append_compaction(replacements, boundary, summary)
        self.conversation = candidate
        return boundary

    def tool_presentation(self, tool_use_id: str) -> ToolResultPresentation | None:
        return self._tool_presentations.get(tool_use_id)

    def _repair_trailing_tool_calls(self) -> None:
        repairs = _trailing_tool_repairs(self.history)
        if repairs is None:
            return
        source = self.history[-1]
        assert isinstance(source, AssistantMessage)
        self.append_tool_results(repairs, source)


def _trailing_tool_repairs(
    messages: tuple[ConversationMessage, ...],
) -> tuple[ToolResult, ...] | None:
    if not messages or not isinstance(messages[-1], AssistantMessage):
        return None
    calls = tuple(
        block for block in messages[-1].content if isinstance(block, ToolCall)
    )
    if not calls:
        return None
    return tuple(
        ToolResult(
            tool_use_id=call.id,
            content="Tool execution was interrupted before the session resumed.",
            is_error=True,
        )
        for call in calls
    )


__all__ = [
    "Session",
]
