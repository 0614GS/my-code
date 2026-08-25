"""Public Session boundary over private conversation and JSONL persistence."""

from collections.abc import Iterable
from pathlib import Path

from my_code.context.session import AttachmentDerivationState, ContextPlanningState
from my_code.conversation.attachments import (
    AttachmentPayload,
    InvokedSkillsAttachment,
    SkillActivationAttachment,
    is_durable_attachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import generic_tool_result_presentation
from my_code.conversation.state import CompactBoundary, ContentReplacement
from my_code.model.primitives import ProviderReplayRecord
from my_code.sessions._aggregate import ConversationAggregate
from my_code.sessions._store import SessionStore
from my_code.sessions._tool_results import ToolResultStore
from my_code.sessions.models import SessionStart


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
            loaded.conversation,
            content_replacements=loaded.content_replacements,
            compact_boundaries=loaded.compact_boundaries,
        )
        self._replay_records = {
            (record.entry_id, record.content_id): record
            for record in loaded.replay_records
        }
        self._tool_results = ToolResultStore(
            project_state_dir / session_id / "tool-results"
        )
        self._repair_trailing_tool_calls()

    @classmethod
    def restore(cls, project_state_dir: Path, session_id: str) -> "Session":
        candidate = cls(project_state_dir, session_id)
        if not candidate.conversation:
            raise ValueError(f"Session contains no messages: {session_id}")
        return candidate

    @property
    def session_id(self) -> str:
        return self._store.session_id

    @property
    def conversation(self) -> tuple[ConversationEntry, ...]:
        return self._conversation.conversation

    @property
    def context_entries(self) -> tuple[ConversationEntry, ...]:
        return self._conversation.context_entries

    def context_planning_state(self) -> ContextPlanningState:
        context_ids = {entry.uuid for entry in self.context_entries}
        return ContextPlanningState(
            context_entries=self.context_entries,
            content_replacements=self._conversation.content_replacements,
            replay_records=tuple(
                record
                for record in self._replay_records.values()
                if record.entry_id in context_ids
            ),
        )

    def attachment_derivation_state(self) -> AttachmentDerivationState:
        return AttachmentDerivationState(
            self.session_id, self.conversation, self.context_entries
        )

    @property
    def causal_head_uuid(self) -> str | None:
        return next(
            (
                entry.uuid
                for entry in reversed(self._conversation.conversation)
                if not isinstance(entry, AttachmentMessage)
                or is_durable_attachment(entry.payload)
            ),
            None,
        )

    def append_attachment(self, payload: AttachmentPayload) -> AttachmentMessage:
        message = AttachmentMessage(payload, parent_uuid=self.causal_head_uuid)
        self._commit_entry(message)
        return message

    @property
    def context_entry_count(self) -> int:
        return len(self._conversation.context_entries)

    @property
    def conversation_entry_count(self) -> int:
        return len(self._conversation.conversation)

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

    def append_assistant_message(
        self,
        message: AssistantMessage,
        *,
        replay_records: tuple[ProviderReplayRecord, ...] = (),
    ) -> None:
        if not isinstance(message, AssistantMessage):
            raise TypeError("append_assistant_message requires AssistantMessage")
        self._commit_entry(message, replay_records=replay_records)

    def append_tool_result_batch(
        self,
        batch: ToolResultBatch,
    ) -> ToolResultBatch:
        if not isinstance(batch, ToolResultBatch):
            raise TypeError("append_tool_result_batch requires ToolResultBatch")
        existing_files = self._tool_result_files()
        try:
            persisted = self._externalize_tool_result_batch(batch)
            self._commit_entry(persisted)
        except BaseException:
            self._rollback_tool_result_files(existing_files)
            raise
        return persisted

    def commit_tool_round(
        self,
        batch: ToolResultBatch,
        attachments: tuple[AttachmentPayload, ...] = (),
    ) -> tuple[ToolResultBatch, tuple[AttachmentMessage, ...]]:
        """Commit a closed result batch before all ordered tool follow-ups."""

        if not isinstance(batch, ToolResultBatch):
            raise TypeError("commit_tool_round requires ToolResultBatch")
        existing_files = self._tool_result_files()
        try:
            persisted = self._externalize_tool_result_batch(batch)
            candidate = self._conversation.clone()
            candidate.append(persisted)
            messages: list[AttachmentMessage] = []
            for payload in attachments:
                attachment = AttachmentMessage(
                    payload, parent_uuid=_causal_head(candidate.conversation)
                )
                candidate.append(attachment)
                messages.append(attachment)
            durable = tuple(
                message
                for message in messages
                if is_durable_attachment(message.payload)
            )
            self._store.append_message_batch((persisted, *durable))
        except BaseException:
            self._rollback_tool_result_files(existing_files)
            raise
        self._conversation = candidate
        return persisted, tuple(messages)

    def _tool_result_files(self) -> frozenset[Path]:
        root = self._tool_results.root
        if not root.exists():
            return frozenset()
        return frozenset(path for path in root.iterdir() if path.is_file())

    def _rollback_tool_result_files(self, existing: frozenset[Path]) -> None:
        root = self._tool_results.root
        if not root.exists():
            return
        for path in root.iterdir():
            if path.is_file() and path not in existing:
                path.unlink()

    def _externalize_tool_result_batch(self, batch: ToolResultBatch) -> ToolResultBatch:
        results = tuple(
            ToolResult(
                tool_use_id=result.tool_use_id,
                content=self._tool_results.externalize(
                    result.tool_use_id, result.content
                ),
                presentation=result.presentation,
                is_error=result.is_error,
            )
            for result in batch.content
        )
        if results == batch.content:
            return batch
        return ToolResultBatch(
            content=results,
            source_assistant_id=batch.source_assistant_id,
            uuid=batch.uuid,
            parent_uuid=batch.parent_uuid,
            timestamp=batch.timestamp,
        )

    def _commit_entry(
        self,
        entry: ConversationEntry,
        *,
        replay_records: tuple[ProviderReplayRecord, ...] = (),
    ) -> None:
        candidate = self._conversation.clone()
        changed = candidate.append(entry)
        if not changed:
            return
        durable = not isinstance(entry, AttachmentMessage) or is_durable_attachment(
            entry.payload
        )
        if durable:
            if not self._store.append_message(entry, replay_records=replay_records):
                raise ValueError(
                    "Entry UUID already exists outside the active conversation: "
                    f"{entry.uuid}"
                )
        self._conversation = candidate
        self._replay_records.update(
            ((record.entry_id, record.content_id), record) for record in replay_records
        )

    def append_tool_results(
        self,
        results: Iterable[ToolResult],
        assistant_message: AssistantMessage,
    ) -> ToolResultBatch:
        result_blocks = tuple(results)
        if not result_blocks:
            raise ValueError("A tool result message must contain at least one result")
        message = ToolResultBatch(
            content=result_blocks,
            parent_uuid=assistant_message.uuid,
            source_assistant_id=assistant_message.uuid,
        )
        return self.append_tool_result_batch(message)

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
        invoked = _latest_invoked_skills(candidate.conversation[:-1])
        attachments: tuple[AttachmentMessage, ...] = ()
        if invoked is not None:
            attachment = AttachmentMessage(invoked, parent_uuid=summary.uuid)
            candidate.append(attachment)
            attachments = (attachment,)
        self._store.append_compaction(replacements, boundary, summary, attachments)
        self._conversation = candidate
        return boundary

    def _repair_trailing_tool_calls(self) -> None:
        conversation = self._conversation.conversation
        repairs = _trailing_tool_repairs(conversation)
        if repairs is None:
            return
        source = conversation[-1]
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
            presentation=generic_tool_result_presentation(
                "Tool execution was interrupted before the session resumed.", True
            ),
            is_error=True,
        )
        for call in calls
    )


def _latest_invoked_skills(
    history: tuple[ConversationEntry, ...],
) -> InvokedSkillsAttachment | None:
    by_name: dict[str, SkillActivationAttachment] = {}
    for entry in history:
        if not isinstance(entry, AttachmentMessage):
            continue
        payload = entry.payload
        if isinstance(payload, SkillActivationAttachment):
            by_name[payload.name] = payload
        elif isinstance(payload, InvokedSkillsAttachment):
            for skill in payload.skills:
                by_name[skill.name] = skill
    if not by_name:
        return None
    return InvokedSkillsAttachment(tuple(by_name.values()))


def _causal_head(history: tuple[ConversationEntry, ...]) -> str | None:
    return next(
        (
            entry.uuid
            for entry in reversed(history)
            if not isinstance(entry, AttachmentMessage)
            or is_durable_attachment(entry.payload)
        ),
        None,
    )


__all__ = [
    "Session",
]
