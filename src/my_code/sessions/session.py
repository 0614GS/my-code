"""Public Session boundary over private conversation and JSONL persistence."""

from collections.abc import Iterable
from pathlib import Path

from my_code.conversation.models import (
    AssistantMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.state import CompactBoundary, ContentReplacement
from my_code.sessions._aggregate import ConversationAggregate
from my_code.sessions.models import SessionSnapshot, SessionStart
from my_code.sessions.store import SessionStore
from my_code.tools.presentation import ToolResultPresentation


class Session:
    """Own one Conversation and commit recoverable changes persistence-first."""

    def __init__(
        self,
        project_state_dir: Path,
        session_id: str,
        *,
        start: SessionStart | None = None,
    ) -> None:
        self._store = SessionStore(project_state_dir, session_id, start=start)
        loaded = self._store.load()
        self._conversation = ConversationAggregate(
            loaded.history,
            content_replacements=loaded.content_replacements,
            compact_boundaries=loaded.compact_boundaries,
        )
        self._tool_presentations = dict(loaded.tool_presentations)
        self._repair_trailing_tool_calls()

    @classmethod
    def restore(cls, project_state_dir: Path, session_id: str) -> "Session":
        candidate = cls(project_state_dir, session_id)
        if not candidate.snapshot().history:
            raise ValueError(f"Session contains no messages: {session_id}")
        return candidate

    @property
    def session_id(self) -> str:
        return self._store.session_id

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            history=self._conversation.history,
            working_set=self._conversation.working_set,
            content_replacements=self._conversation.content_replacements,
            compact_boundaries=self._conversation.compact_boundaries,
            tool_presentations=tuple(self._tool_presentations.items()),
        )

    @property
    def message_count(self) -> int:
        return len(self._conversation.working_set)

    @property
    def history_message_count(self) -> int:
        return len(self._conversation.history)

    @property
    def content_replacement_count(self) -> int:
        return len(self._conversation.content_replacements)

    @property
    def compact_count(self) -> int:
        return len(self._conversation.compact_boundaries)

    def append_human_message(self, message: HumanMessage) -> None:
        if not isinstance(message, HumanMessage):
            raise TypeError("append_human_message requires HumanMessage")
        self._commit_entry(message)

    def append_assistant_message(self, message: AssistantMessage) -> None:
        if not isinstance(message, AssistantMessage):
            raise TypeError("append_assistant_message requires AssistantMessage")
        self._commit_entry(message)

    def append_tool_result_batch(
        self,
        batch: ToolResultBatch,
        *,
        presentations: Iterable[tuple[str, ToolResultPresentation]] = (),
    ) -> None:
        if not isinstance(batch, ToolResultBatch):
            raise TypeError("append_tool_result_batch requires ToolResultBatch")
        self._commit_entry(batch, presentations=presentations)

    def _commit_entry(
        self,
        entry: ConversationEntry,
        *,
        presentations: Iterable[tuple[str, ToolResultPresentation]] = (),
    ) -> None:
        candidate = self._conversation.clone()
        changed = candidate.append(entry)
        if not changed:
            return
        presentation_items = tuple(presentations)
        if not self._store.append_message(entry, presentation_items):
            raise ValueError(
                f"Entry UUID already exists outside the active conversation: "
                f"{entry.uuid}"
            )
        self._conversation = candidate
        self._tool_presentations.update(presentation_items)

    def append_tool_results(
        self,
        results: Iterable[ToolResult],
        assistant_message: AssistantMessage,
        *,
        presentations: Iterable[tuple[str, ToolResultPresentation]] = (),
    ) -> ToolResultBatch:
        result_blocks = tuple(results)
        if not result_blocks:
            raise ValueError("A tool result message must contain at least one result")
        message = ToolResultBatch(
            content=result_blocks,
            parent_uuid=assistant_message.uuid,
            source_assistant_id=assistant_message.uuid,
        )
        self.append_tool_result_batch(message, presentations=presentations)
        return message

    def commit_content_replacement(self, replacement: ContentReplacement) -> None:
        candidate = self._conversation.clone()
        if not candidate.add_content_replacement(replacement):
            return
        self._store.append_content_replacement(replacement)
        self._conversation = candidate

    def commit_compaction(
        self,
        replacements: tuple[ContentReplacement, ...],
        summary: ConversationSummaryMessage,
        boundary: CompactBoundary,
    ) -> CompactBoundary:
        candidate = self._conversation.clone()
        for replacement in replacements:
            candidate.add_content_replacement(replacement)
        candidate.add_compact_boundary(boundary)
        candidate.append(summary)
        self._store.append_compaction(replacements, boundary, summary)
        self._conversation = candidate
        return boundary

    def tool_presentation(self, tool_use_id: str) -> ToolResultPresentation | None:
        return self._tool_presentations.get(tool_use_id)

    def _repair_trailing_tool_calls(self) -> None:
        history = self._conversation.history
        repairs = _trailing_tool_repairs(history)
        if repairs is None:
            return
        source = history[-1]
        assert isinstance(source, AssistantMessage)
        self.append_tool_results(repairs, source)


def _trailing_tool_repairs(
    messages: tuple[ConversationEntry, ...],
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
