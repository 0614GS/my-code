"""Public Session boundary over private conversation and JSONL persistence."""

import hashlib
import os
import tempfile
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from my_code.context.session import AttachmentDerivationState, ContextPlanningState
from my_code.conversation.attachments import (
    AttachmentPayload,
    CollaborationModeAttachment,
    InvokedSkillsAttachment,
    SkillActivationAttachment,
    ToolDiscoveryAttachment,
    ToolDiscoveryDefinition,
    ToolDiscoveryInvalidationAttachment,
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
from my_code.model.invocation import (
    ModelInvocation,
    ModelInvocationReceipt,
    ModelInvocationRecorder,
    ModelInvocationStatus,
)
from my_code.model.primitives import ProviderReplayRecord
from my_code.sessions._aggregate import ConversationAggregate
from my_code.sessions._request_audit import RequestAuditStore
from my_code.sessions._store import SessionStore
from my_code.sessions._tool_results import ToolResultStore
from my_code.sessions.models import (
    SessionStart,
    TurnFinished,
    TurnHistoryEntry,
    TurnStarted,
)
from my_code.sessions.request_audit import RequestAuditSnapshot


class Session(ModelInvocationRecorder):
    """Own one Conversation and commit recoverable changes persistence-first."""

    def __init__(
        self,
        project_state_dir: Path,
        session_id: str,
        *,
        tool_results_dir: Path | None = None,
        start: SessionStart | None = None,
    ) -> None:
        self._store = SessionStore(project_state_dir, session_id, start=start)
        loaded = self._store.load()
        self._request_audit = RequestAuditStore(self._store.session_dir)
        self._audit_legacy_gap = bool(loaded.conversation) and (
            self._request_audit.snapshot().legacy_missing
        )
        self._conversation = ConversationAggregate(
            loaded.conversation,
            content_replacements=loaded.content_replacements,
            compact_boundaries=loaded.compact_boundaries,
        )
        self._replay_records = {
            (record.entry_id, record.content_id): record
            for record in loaded.replay_records
        }
        self._turn_history = list(loaded.turn_history)
        self._tool_results = ToolResultStore(
            tool_results_dir
            if tool_results_dir is not None
            else _default_tool_results_dir(project_state_dir, session_id)
        )
        self._repair_trailing_tool_calls()

    def prepare_model_invocation(
        self, invocation: ModelInvocation
    ) -> ModelInvocationReceipt:
        """Durably record a semantic request before any provider delivery."""

        manifest = self._request_audit.prepare(invocation)
        return ModelInvocationReceipt(
            manifest.request_id,
            manifest.request_number,
            manifest.input_refs,
        )

    def finish_model_invocation(
        self,
        request_id: str,
        status: ModelInvocationStatus,
        error: str | None = None,
    ) -> None:
        self._request_audit.finish(request_id, status, error)

    def request_audit_snapshot(self) -> RequestAuditSnapshot:
        snapshot = self._request_audit.snapshot()
        return replace(snapshot, legacy_missing=self._audit_legacy_gap)

    @classmethod
    def restore(
        cls,
        project_state_dir: Path,
        session_id: str,
        *,
        tool_results_dir: Path | None = None,
    ) -> "Session":
        candidate = cls(
            project_state_dir, session_id, tool_results_dir=tool_results_dir
        )
        if not candidate.conversation:
            raise ValueError(f"Session contains no messages: {session_id}")
        return candidate

    @property
    def session_id(self) -> str:
        return self._store.session_id

    @property
    def start(self) -> SessionStart:
        return self._store.start

    @property
    def permission_mode(self) -> str:
        return self._store.permission_mode

    @property
    def collaboration_mode(self) -> str:
        return self._store.collaboration_mode

    @property
    def turn_history(self) -> tuple[TurnHistoryEntry, ...]:
        """Return journal entries in turn start order."""

        return tuple(self._turn_history)

    def append_turn_started(self, turn: TurnStarted) -> bool:
        appended = self._store.append_turn_started(turn)
        if appended:
            self._turn_history.append(TurnHistoryEntry(turn))
        return appended

    def append_turn_finished(self, turn: TurnFinished) -> bool:
        appended = self._store.append_turn_finished(turn)
        if appended:
            for index, item in enumerate(self._turn_history):
                if item.started.turn_id == turn.turn_id:
                    self._turn_history[index] = TurnHistoryEntry(item.started, turn)
                    break
            else:
                raise AssertionError("Persisted turn finish has no in-memory start")
        return appended

    def configure_start(self, start: SessionStart) -> None:
        self._store.configure_start(start)

    def set_permission_mode(self, permission_mode: str) -> bool:
        return self._store.set_permission_mode(permission_mode)

    def set_collaboration_mode(self, collaboration_mode: str) -> bool:
        return self._store.set_collaboration_mode(collaboration_mode)

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

    def commit_user_inputs(
        self,
        inputs: Iterable[tuple[str, tuple[AttachmentPayload, ...]]],
        *,
        prelude: tuple[AttachmentPayload, ...] = (),
    ) -> tuple[HumanMessage, ...]:
        """Atomically commit adjacent user messages and durable attachments.

        The complete candidate is validated before one JSONL batch append.  The
        in-memory aggregate is published only after persistence succeeds.
        """

        values = tuple(inputs)
        if not values:
            return ()
        candidate = self._conversation.clone()
        durable: list[ConversationEntry] = []
        humans: list[HumanMessage] = []
        for payload in prelude:
            attachment = AttachmentMessage(
                payload, parent_uuid=_causal_head(candidate.conversation)
            )
            candidate.append(attachment)
            if is_durable_attachment(payload):
                durable.append(attachment)
        for prompt, attachments in values:
            human = HumanMessage(
                prompt, parent_uuid=_causal_head(candidate.conversation)
            )
            candidate.append(human)
            durable.append(human)
            humans.append(human)
            for payload in attachments:
                attachment = AttachmentMessage(
                    payload, parent_uuid=_causal_head(candidate.conversation)
                )
                candidate.append(attachment)
                if is_durable_attachment(payload):
                    durable.append(attachment)
        self._store.append_message_batch(tuple(durable))
        self._conversation = candidate
        return tuple(humans)

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
        discovered = _latest_tool_discoveries(candidate.conversation[:-1])
        collaboration = _latest_collaboration_mode(candidate.conversation[:-1])
        attachments_list: list[AttachmentMessage] = []
        parent_uuid = summary.uuid
        if collaboration is not None:
            attachment = AttachmentMessage(collaboration, parent_uuid=parent_uuid)
            candidate.append(attachment)
            attachments_list.append(attachment)
            parent_uuid = attachment.uuid
        if invoked is not None:
            attachment = AttachmentMessage(invoked, parent_uuid=parent_uuid)
            candidate.append(attachment)
            attachments_list.append(attachment)
            parent_uuid = attachment.uuid
        if discovered is not None:
            attachment = AttachmentMessage(discovered, parent_uuid=parent_uuid)
            candidate.append(attachment)
            attachments_list.append(attachment)
        attachments = tuple(attachments_list)
        self._store.append_compaction(replacements, boundary, summary, attachments)
        self._conversation = candidate
        return boundary

    def _repair_trailing_tool_calls(self) -> None:
        self.close_unresolved_tool_calls(
            "Tool execution was interrupted before the session resumed."
        )

    def close_unresolved_tool_calls(self, message: str) -> ToolResultBatch | None:
        """Idempotently close a trailing assistant tool-use message."""

        conversation = self._conversation.conversation
        repairs = _trailing_tool_repairs(conversation, message)
        if repairs is None:
            return None
        source = next(
            entry
            for entry in reversed(conversation)
            if isinstance(entry, AssistantMessage)
        )
        return self.append_tool_results(repairs, source)


def _trailing_tool_repairs(
    messages: tuple[ConversationEntry, ...],
    message: str,
) -> tuple[ToolResult, ...] | None:
    meaningful = tuple(
        entry for entry in messages if not isinstance(entry, AttachmentMessage)
    )
    if not meaningful or not isinstance(meaningful[-1], AssistantMessage):
        return None
    calls = tuple(
        block for block in meaningful[-1].content if isinstance(block, ToolCall)
    )
    if not calls:
        return None
    return tuple(
        ToolResult(
            tool_use_id=call.id,
            content=message,
            presentation=generic_tool_result_presentation(message, True),
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


def _latest_collaboration_mode(
    history: tuple[ConversationEntry, ...],
) -> CollaborationModeAttachment | None:
    for entry in reversed(history):
        if isinstance(entry, AttachmentMessage) and isinstance(
            entry.payload, CollaborationModeAttachment
        ):
            return entry.payload
    return None


def _latest_tool_discoveries(
    history: tuple[ConversationEntry, ...],
) -> ToolDiscoveryAttachment | None:
    by_name: dict[str, ToolDiscoveryDefinition] = {}
    mode: str = "dispatcher"
    for entry in history:
        if not isinstance(entry, AttachmentMessage):
            continue
        payload = entry.payload
        if isinstance(payload, ToolDiscoveryAttachment):
            mode = payload.mode
            by_name.update((item.name, item) for item in payload.definitions)
        elif isinstance(payload, ToolDiscoveryInvalidationAttachment):
            for name in payload.names:
                by_name.pop(name, None)
    if not by_name:
        return None
    return ToolDiscoveryAttachment(
        tuple(by_name[name] for name in sorted(by_name)), mode
    )


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


def _default_tool_results_dir(project_state_dir: Path, session_id: str) -> Path:
    """Keep direct Session consumers ephemeral even without application paths."""

    uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
    digest = hashlib.sha256(
        str(project_state_dir.resolve(strict=False)).encode("utf-8")
    ).hexdigest()[:20]
    return (
        Path(tempfile.gettempdir())
        / f"my-code-{uid}"
        / f"session-store-{digest}"
        / session_id
        / "tool-results"
    )


__all__ = [
    "Session",
]
