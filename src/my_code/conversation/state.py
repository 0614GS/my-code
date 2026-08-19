"""Pure in-memory conversation aggregate and compaction facts."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from my_code.conversation.models import (
    AssistantMessage,
    ConversationMessage,
    ToolCall,
    ToolResultsMessage,
)

type CompactTrigger = Literal["auto", "manual", "reactive"]


@dataclass(frozen=True, slots=True)
class ContentReplacement:
    tool_use_id: str
    tool_name: str
    original_chars: int
    content: str

    def __post_init__(self) -> None:
        if not self.tool_use_id or not self.tool_name or not self.content:
            raise ValueError("Content replacement strings must not be empty")
        if self.original_chars < 1:
            raise ValueError("original_chars must be positive")

    @classmethod
    def for_tool_result(
        cls, *, tool_use_id: str, tool_name: str, original_chars: int
    ) -> "ContentReplacement":
        return cls(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            original_chars=original_chars,
            content=(
                f"[Previous {tool_name} result compacted: {original_chars} chars. "
                "Run the tool again if the full output is needed.]"
            ),
        )


@dataclass(frozen=True, slots=True)
class CompactBoundary:
    parent_uuid: str
    summary_uuid: str
    trigger: CompactTrigger
    pre_compact_chars: int
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if self.pre_compact_chars < 1:
            raise ValueError("pre_compact_chars must be positive")


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """Immutable conversation facts consumed by Context."""

    messages: tuple[ConversationMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = field(default_factory=tuple)
    session_history: tuple[ConversationMessage, ...] = field(default_factory=tuple)


class Conversation:
    """Own message-chain and working-set invariants without performing I/O."""

    def __init__(
        self,
        history: Iterable[ConversationMessage] = (),
        *,
        content_replacements: Iterable[ContentReplacement] = (),
        compact_boundaries: Iterable[CompactBoundary] = (),
    ) -> None:
        self._history: tuple[ConversationMessage, ...] = ()
        self._replacements: dict[str, ContentReplacement] = {}
        self._boundaries: dict[str, CompactBoundary] = {}
        for message in history:
            self.append(message)
        for replacement in content_replacements:
            self.add_content_replacement(replacement)
        for boundary in compact_boundaries:
            self.add_compact_boundary(boundary)

    def clone(self) -> "Conversation":
        return Conversation(
            self._history,
            content_replacements=self._replacements.values(),
            compact_boundaries=self._boundaries.values(),
        )

    @property
    def history(self) -> tuple[ConversationMessage, ...]:
        return self._history

    @property
    def working_set(self) -> tuple[ConversationMessage, ...]:
        boundaries = self.compact_boundaries
        if not boundaries:
            return self._history
        summary_uuid = boundaries[-1].summary_uuid
        for index, message in enumerate(self._history):
            if message.uuid == summary_uuid:
                return self._history[index:]
        return self._history

    @property
    def content_replacements(self) -> tuple[ContentReplacement, ...]:
        working_tool_ids = {
            result.tool_use_id
            for message in self.working_set
            if isinstance(message, ToolResultsMessage)
            for result in message.content
        }
        return tuple(
            item
            for item in self._replacements.values()
            if item.tool_use_id in working_tool_ids
        )

    @property
    def all_content_replacements(self) -> tuple[ContentReplacement, ...]:
        return tuple(self._replacements.values())

    @property
    def compact_boundaries(self) -> tuple[CompactBoundary, ...]:
        active_ids = {message.uuid for message in self._history}
        return tuple(
            item
            for item in self._boundaries.values()
            if item.parent_uuid in active_ids and item.summary_uuid in active_ids
        )

    def snapshot(self) -> ConversationSnapshot:
        return ConversationSnapshot(
            messages=self.working_set,
            content_replacements=self.content_replacements,
            session_history=self._history,
        )

    def append(self, message: ConversationMessage) -> bool:
        for existing in self._history:
            if existing.uuid != message.uuid:
                continue
            if existing != message:
                raise ValueError(f"Conflicting message UUID: {message.uuid}")
            return False
        if message.parent_uuid is None:
            candidate = (message,)
        else:
            parent_index = next(
                (
                    index
                    for index, existing in enumerate(self._history)
                    if existing.uuid == message.parent_uuid
                ),
                None,
            )
            if parent_index is None:
                raise ValueError(
                    f"Parent is not in the active conversation: {message.parent_uuid}"
                )
            candidate = self._history[: parent_index + 1] + (message,)
        _validate_tool_result(message, candidate)
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
        if not any(item.uuid == boundary.parent_uuid for item in self._history):
            raise ValueError(f"Unknown compact parent UUID: {boundary.parent_uuid}")
        previous = self._boundaries.get(boundary.id)
        if previous is not None:
            if previous != boundary:
                raise ValueError(f"Conflicting compact boundary: {boundary.id}")
            return False
        self._boundaries[boundary.id] = boundary
        return True


def _validate_tool_result(
    message: ConversationMessage, history: tuple[ConversationMessage, ...]
) -> None:
    if not isinstance(message, ToolResultsMessage):
        return
    source = next(
        (
            item
            for item in history
            if item.uuid == message.source_assistant_uuid
            and isinstance(item, AssistantMessage)
        ),
        None,
    )
    if source is None or message.parent_uuid != source.uuid:
        raise ValueError("Tool results must directly follow their source assistant")
    expected = {block.id for block in source.content if isinstance(block, ToolCall)}
    actual = {block.tool_use_id for block in message.content}
    if actual != expected:
        raise ValueError("Tool results do not match source tool calls")


__all__ = [
    "CompactBoundary",
    "CompactTrigger",
    "ContentReplacement",
    "Conversation",
    "ConversationSnapshot",
]
