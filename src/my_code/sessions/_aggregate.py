"""Private in-memory aggregate used only by Session."""

from collections.abc import Iterable

from my_code.conversation.attachments import is_durable_attachment
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationEntry,
    ToolCall,
    ToolResultBatch,
)
from my_code.conversation.state import CompactBoundary, ContentReplacement


class ConversationAggregate:
    def __init__(
        self,
        conversation: Iterable[ConversationEntry] = (),
        *,
        content_replacements: Iterable[ContentReplacement] = (),
        compact_boundaries: Iterable[CompactBoundary] = (),
    ) -> None:
        self._history: tuple[ConversationEntry, ...] = ()
        self._replacements: dict[str, ContentReplacement] = {}
        self._boundaries: dict[str, CompactBoundary] = {}
        for entry in conversation:
            self.append(entry)
        for replacement in content_replacements:
            self.add_content_replacement(replacement)
        for boundary in compact_boundaries:
            self.add_compact_boundary(boundary)

    def clone(self) -> "ConversationAggregate":
        return ConversationAggregate(
            self._history,
            content_replacements=self._replacements.values(),
            compact_boundaries=self._boundaries.values(),
        )

    @property
    def conversation(self) -> tuple[ConversationEntry, ...]:
        return self._history

    @property
    def context_entries(self) -> tuple[ConversationEntry, ...]:
        boundaries = self.compact_boundaries
        if not boundaries:
            return self._history
        summary_id = boundaries[-1].summary_uuid
        for index, entry in enumerate(self._history):
            if entry.uuid == summary_id:
                return self._history[index:]
        return self._history

    @property
    def content_replacements(self) -> tuple[ContentReplacement, ...]:
        working_tool_ids = {
            result.tool_use_id
            for entry in self.context_entries
            if isinstance(entry, ToolResultBatch)
            for result in entry.content
        }
        return tuple(
            replacement
            for replacement in self._replacements.values()
            if replacement.tool_use_id in working_tool_ids
        )

    @property
    def compact_boundaries(self) -> tuple[CompactBoundary, ...]:
        active_ids = {entry.uuid for entry in self._history}
        return tuple(
            boundary
            for boundary in self._boundaries.values()
            if boundary.parent_uuid in active_ids
            and boundary.summary_uuid in active_ids
        )

    def append(self, entry: ConversationEntry) -> bool:
        for existing in self._history:
            if existing.uuid != entry.uuid:
                continue
            if existing != entry:
                raise ValueError(f"Conflicting entry UUID: {entry.uuid}")
            return False
        if entry.parent_uuid is None:
            candidate = (entry,)
        else:
            parent_index = next(
                (
                    index
                    for index, existing in enumerate(self._history)
                    if existing.uuid == entry.parent_uuid
                ),
                None,
            )
            if parent_index is None:
                raise ValueError(
                    f"Parent is not in the active conversation: {entry.parent_uuid}"
                )
            suffix = self._history[parent_index + 1 :]
            transient_suffix = bool(suffix) and all(
                isinstance(item, AttachmentMessage)
                and not is_durable_attachment(item.payload)
                for item in suffix
            )
            candidate = (
                self._history + (entry,)
                if transient_suffix
                else self._history[: parent_index + 1] + (entry,)
            )
        _validate_protocol_transition(entry, self._history)
        _validate_tool_result(entry, candidate)
        self._history = candidate
        return True

    def add_content_replacement(self, replacement: ContentReplacement) -> bool:
        previous = self._replacements.get(replacement.tool_use_id)
        if previous is not None:
            if previous != replacement:
                raise ValueError(
                    f"Conflicting content replacement: {replacement.tool_use_id}"
                )
            return False
        self._replacements[replacement.tool_use_id] = replacement
        return True

    def add_compact_boundary(self, boundary: CompactBoundary) -> bool:
        if not any(entry.uuid == boundary.parent_uuid for entry in self._history):
            raise ValueError(f"Unknown compact parent UUID: {boundary.parent_uuid}")
        previous = self._boundaries.get(boundary.id)
        if previous is not None:
            if previous != boundary:
                raise ValueError(f"Conflicting compact boundary: {boundary.id}")
            return False
        self._boundaries[boundary.id] = boundary
        return True


def _validate_tool_result(
    entry: ConversationEntry, history: tuple[ConversationEntry, ...]
) -> None:
    if not isinstance(entry, ToolResultBatch):
        return
    source = next(
        (
            item
            for item in history
            if item.uuid == entry.source_assistant_id
            and isinstance(item, AssistantMessage)
        ),
        None,
    )
    if source is None or entry.parent_uuid != source.uuid:
        raise ValueError("Tool results must directly follow their source assistant")
    expected = {block.id for block in source.content if isinstance(block, ToolCall)}
    actual = {block.tool_use_id for block in entry.content}
    if actual != expected:
        raise ValueError("Tool results do not match source tool calls")


def _validate_protocol_transition(
    entry: ConversationEntry, history: tuple[ConversationEntry, ...]
) -> None:
    if not history:
        return
    previous = history[-1]
    if entry.parent_uuid != previous.uuid:
        return
    if not isinstance(previous, AssistantMessage):
        return
    has_calls = any(isinstance(block, ToolCall) for block in previous.content)
    if has_calls and not isinstance(entry, ToolResultBatch):
        raise ValueError(
            "Nothing may be inserted between assistant tool calls and their results"
        )


__all__: list[str] = []
