"""Session 私有 TranscriptEntry JSON codec。"""

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from my_code.conversation.attachments import (
    AttachmentPayload,
    BackgroundTaskCompletionAttachment,
    FileMentionAttachment,
    InvokedSkillsAttachment,
    SkillActivationAttachment,
    SkillListingAttachment,
    SkillListingEntry,
    TodoReminderAttachment,
    ToolDiscoveryAttachment,
    ToolDiscoveryDefinition,
    ToolDiscoveryInvalidationAttachment,
    ToolSearchListingAttachment,
)
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
from my_code.conversation.presentation import (
    ToolResultPresentation,
    generic_tool_result_presentation,
)
from my_code.conversation.state import CompactBoundary, ContentReplacement
from my_code.foundation.json import JsonObject, to_json_object
from my_code.model.capabilities import ModelLimits
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ProviderReplayRecord,
    ReasoningPresentation,
    TokenUsage,
    replay_content_id,
    validate_provider_id,
)
from my_code.sessions._records import (
    AssistantMessageRecord,
    AttachmentMessageRecord,
    CompactBoundaryRecord,
    ContentReplacementRecord,
    ConversationSummaryMessageRecord,
    HumanMessageRecord,
    LegacyToolResultBatchRecord,
    MessageRecord,
    ProviderReplaySidecarRecord,
    ReasoningContentRecord,
    SessionMetadataRecord,
    SessionStartedRecord,
    TextContentRecord,
    ToolCallRecord,
    ToolPresentationRecord,
    ToolResultBatchRecord,
    ToolResultRecord,
    TranscriptEntry,
)
from my_code.sessions.models import SessionMetadata, SessionStart


class TranscriptDecodeError(ValueError):
    pass


type DecodedEntry = (
    ConversationEntry
    | ContentReplacement
    | CompactBoundary
    | SessionStart
    | SessionMetadata
    | ToolPresentationRecord
    | ProviderReplayRecord
)


def decode_entry(value: object) -> DecodedEntry:
    """将不可信 JSON 一步映射为 sessions port 可见的领域值。"""

    entry = entry_from_json(value)
    if isinstance(entry, SessionStartedRecord):
        return SessionStart(
            entry.session_id,
            entry.created_at,
            entry.cwd,
            entry.provider_id,
            entry.model,
            entry.permission_mode,
            entry.max_steps,
            entry.max_output_tokens,
            entry.context_chars,
            entry.model_limits,
            entry.model_limit_source,
            entry.compact_trigger_tokens,
            entry.provider_protocol,
        )
    if isinstance(entry, SessionMetadataRecord):
        return SessionMetadata(
            entry.created_at, entry.updated_at, entry.title, entry.last_prompt
        )
    if isinstance(entry, ContentReplacementRecord):
        return ContentReplacement(
            entry.tool_use_id,
            entry.tool_name,
            entry.original_chars,
            entry.content,
        )
    if isinstance(entry, CompactBoundaryRecord):
        return CompactBoundary(
            entry.parent_uuid,
            entry.summary_uuid,
            entry.trigger,
            entry.pre_compact_chars,
            entry.id,
        )
    if isinstance(entry, ToolPresentationRecord):
        return entry
    if isinstance(entry, ProviderReplaySidecarRecord):
        return ProviderReplayRecord(
            entry.entry_id, entry.content_id, entry.continuation
        )
    return record_to_message(entry)


def encode_message(message: ConversationEntry) -> JsonObject:
    return entry_to_json(message_to_record(message))


def encode_replacement(replacement: ContentReplacement) -> JsonObject:
    return entry_to_json(
        ContentReplacementRecord(
            replacement.tool_use_id,
            replacement.tool_name,
            replacement.original_chars,
            replacement.content,
        )
    )


def encode_boundary(boundary: CompactBoundary) -> JsonObject:
    return entry_to_json(
        CompactBoundaryRecord(
            boundary.id,
            boundary.parent_uuid,
            boundary.summary_uuid,
            boundary.trigger,
            boundary.pre_compact_chars,
        )
    )


def encode_start(start: SessionStart) -> JsonObject:
    return entry_to_json(
        SessionStartedRecord(
            start.session_id,
            start.created_at,
            start.cwd,
            start.provider_id,
            start.model,
            start.permission_mode,
            start.max_steps,
            start.max_output_tokens,
            start.context_chars,
            start.model_limits,
            start.model_limit_source,
            start.compact_trigger_tokens,
            start.provider_protocol,
        )
    )


def encode_metadata(metadata: SessionMetadata) -> JsonObject:
    return entry_to_json(
        SessionMetadataRecord(
            metadata.created_at,
            metadata.updated_at,
            metadata.title,
            metadata.last_prompt,
        )
    )


def encode_replay(record: ProviderReplayRecord) -> JsonObject:
    return entry_to_json(
        ProviderReplaySidecarRecord(record.entry_id, record.content_id, record.state)
    )


def presentations_from_json(
    value: object,
) -> tuple[tuple[str, ToolResultPresentation], ...]:
    """Read legacy embedded snapshots without returning them as conversation facts."""

    entry = entry_from_json(value)
    if not isinstance(entry, (LegacyToolResultBatchRecord, ToolResultBatchRecord)):
        return ()
    return tuple(
        (item.tool_use_id, item.presentation)
        for item in entry.content
        if item.presentation is not None
    )


def replay_from_json(value: object) -> tuple[ProviderReplayRecord, ...]:
    """Project legacy embedded continuation fields into replay sidecars."""

    entry = entry_from_json(value)
    if not isinstance(entry, AssistantMessageRecord):
        return ()
    records: list[ProviderReplayRecord] = []
    for index, block in enumerate(entry.content):
        if block.continuation is not None:
            records.append(
                ProviderReplayRecord(
                    entry.uuid,
                    replay_content_id(index),
                    block.continuation,
                )
            )
    return tuple(records)


def message_to_record(message: ConversationEntry) -> MessageRecord:
    if isinstance(message, HumanMessage):
        return HumanMessageRecord(
            message.uuid, message.parent_uuid, message.timestamp, message.content
        )
    if isinstance(message, AssistantMessage):
        assistant_content: tuple[
            TextContentRecord | ToolCallRecord | ReasoningContentRecord, ...
        ] = tuple(
            TextContentRecord(b.text)
            if isinstance(b, TextContent)
            else ToolCallRecord(b.id, b.name, b.input)
            if isinstance(b, ToolCall)
            else ReasoningContentRecord(b.id, b.presentation)
            for b in message.content
        )
        return AssistantMessageRecord(
            message.uuid,
            message.parent_uuid,
            message.timestamp,
            assistant_content,
            message.usage,
            message.provider_binding,
            message.request_input_tokens_estimate,
        )
    if isinstance(message, ToolResultBatch):
        result_content: tuple[ToolResultRecord, ...] = tuple(
            ToolResultRecord(b.tool_use_id, b.content, b.is_error, b.presentation)
            for b in message.content
        )
        return ToolResultBatchRecord(
            message.uuid,
            message.parent_uuid,
            message.timestamp,
            result_content,
            message.source_assistant_id,
        )
    if isinstance(message, AttachmentMessage):
        return AttachmentMessageRecord(
            message.uuid,
            message.parent_uuid,
            message.timestamp,
            _attachment_to_json(message.payload),
        )
    return ConversationSummaryMessageRecord(
        message.uuid, message.parent_uuid, message.timestamp, message.content
    )


def record_to_message(record: MessageRecord) -> ConversationEntry:
    if isinstance(record, HumanMessageRecord):
        return HumanMessage(
            record.content, record.uuid, record.parent_uuid, record.timestamp
        )
    if isinstance(record, AssistantMessageRecord):
        assistant_content: tuple[TextContent | ToolCall | ReasoningContent, ...] = (
            tuple(
                TextContent(b.text)
                if isinstance(b, TextContentRecord)
                else ToolCall(b.id, b.name, b.input)
                if isinstance(b, ToolCallRecord)
                else ReasoningContent(b.id, b.presentation)
                for b in record.content
            )
        )
        return AssistantMessage(
            assistant_content,
            record.usage,
            record.uuid,
            record.parent_uuid,
            record.timestamp,
            record.provider_binding,
            record.request_input_tokens_estimate,
        )
    if isinstance(record, (LegacyToolResultBatchRecord, ToolResultBatchRecord)):
        result_content: tuple[ToolResult, ...] = tuple(
            ToolResult(
                b.tool_use_id,
                b.content,
                b.presentation
                or generic_tool_result_presentation(b.content, b.is_error),
                b.is_error,
            )
            for b in record.content
        )
        return ToolResultBatch(
            result_content,
            (
                record.source_assistant_uuid
                if isinstance(record, LegacyToolResultBatchRecord)
                else record.source_assistant_id
            ),
            record.uuid,
            record.parent_uuid,
            record.timestamp,
        )
    if isinstance(record, AttachmentMessageRecord):
        return AttachmentMessage(
            _attachment_from_json(record.payload),
            record.uuid,
            record.parent_uuid,
            record.timestamp,
        )
    return ConversationSummaryMessage(
        record.content, record.uuid, record.parent_uuid, record.timestamp
    )


def entry_to_json(entry: TranscriptEntry) -> JsonObject:
    base: JsonObject = {"type": entry.type, "schema_version": entry.schema_version}
    if isinstance(entry, SessionStartedRecord):
        base.update(
            session_id=entry.session_id,
            created_at=entry.created_at,
            cwd=entry.cwd,
            provider_id=entry.provider_id,
            model=entry.model,
            permission_mode=entry.permission_mode,
            max_steps=entry.max_steps,
            max_output_tokens=entry.max_output_tokens,
            context_chars=entry.context_chars,
            model_limits={
                "context_window_tokens": entry.model_limits.context_window_tokens,
                "max_input_tokens": entry.model_limits.max_input_tokens,
                "max_output_tokens": entry.model_limits.max_output_tokens,
            },
            model_limit_source=entry.model_limit_source,
            compact_trigger_tokens=entry.compact_trigger_tokens,
        )
        return base
    if isinstance(entry, SessionMetadataRecord):
        base.update(
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            title=entry.title,
            last_prompt=entry.last_prompt,
        )
        return base
    if isinstance(entry, ProviderReplaySidecarRecord):
        base.update(
            entry_id=entry.entry_id,
            content_id=entry.content_id,
            continuation=_continuation_json(entry.continuation),
        )
        return base
    if isinstance(
        entry,
        (
            HumanMessageRecord,
            AssistantMessageRecord,
            LegacyToolResultBatchRecord,
            ToolResultBatchRecord,
            ConversationSummaryMessageRecord,
            AttachmentMessageRecord,
        ),
    ):
        base.update(
            uuid=entry.uuid, parent_uuid=entry.parent_uuid, timestamp=entry.timestamp
        )
    if isinstance(entry, HumanMessageRecord | ConversationSummaryMessageRecord):
        base["content"] = entry.content
    elif isinstance(entry, AttachmentMessageRecord):
        base["payload"] = entry.payload
    elif isinstance(entry, AssistantMessageRecord):
        base["content"] = [_assistant_block_json(b) for b in entry.content]
        base["usage"] = _usage_json(entry.usage)
        if entry.provider_binding is not None:
            base["provider_binding"] = _binding_json(entry.provider_binding)
        if entry.request_input_tokens_estimate is not None:
            base["request_input_tokens_estimate"] = entry.request_input_tokens_estimate
    elif isinstance(entry, LegacyToolResultBatchRecord):
        base["content"] = [_result_json(b) for b in entry.content]
        base["source_assistant_uuid"] = entry.source_assistant_uuid
    elif isinstance(entry, ToolResultBatchRecord):
        base.update(
            uuid=entry.uuid,
            parent_uuid=entry.parent_uuid,
            timestamp=entry.timestamp,
            content=[_result_json(item) for item in entry.content],
            source_assistant_id=entry.source_assistant_id,
        )
    elif isinstance(entry, ContentReplacementRecord):
        base.update(
            tool_use_id=entry.tool_use_id,
            tool_name=entry.tool_name,
            original_chars=entry.original_chars,
            content=entry.content,
        )
    elif isinstance(entry, CompactBoundaryRecord):
        base.update(
            id=entry.id,
            parent_uuid=entry.parent_uuid,
            summary_uuid=entry.summary_uuid,
            trigger=entry.trigger,
            pre_compact_chars=entry.pre_compact_chars,
        )
    else:
        base.update(
            tool_use_id=entry.tool_use_id,
            presentation={
                "summary": entry.presentation.summary,
                "detail": entry.presentation.detail,
                "truncated": entry.presentation.truncated,
            },
        )
    return base


def entry_from_json(value: object) -> TranscriptEntry:
    try:
        data = to_json_object(value)
    except TypeError as error:
        raise TranscriptDecodeError("Transcript entry must be an object") from error
    if data.get("schema_version") != 5:
        raise TranscriptDecodeError("Unsupported transcript schema version")
    kind = _string(data, "type")
    expected_fields = _ENTRY_FIELDS.get(kind)
    if expected_fields is None:
        raise TranscriptDecodeError(f"Unsupported transcript entry type: {kind}")
    actual_expected = expected_fields
    if kind == "assistant_message":
        optional = {"provider_binding", "request_input_tokens_estimate"}
        actual_expected = expected_fields | (frozenset(data) & optional)
    elif kind == "session_started":
        optional = {
            "model_limits",
            "model_limit_source",
            "compact_trigger_tokens",
            "provider_protocol",
        }
        actual_expected = expected_fields | (frozenset(data) & optional)
    _require_exact_fields(data, actual_expected)
    if kind == "session_started":
        cwd = _string(data, "cwd")
        if not Path(cwd).is_absolute():
            raise TranscriptDecodeError("'cwd' must be an absolute path")
        provider_id = _string(data, "provider_id")
        try:
            validate_provider_id(provider_id)
        except ValueError as error:
            raise TranscriptDecodeError(str(error)) from error
        permission_mode = _string(data, "permission_mode")
        if permission_mode not in {
            "default",
            "acceptEdits",
            "plan",
            "dontAsk",
            "bypassPermissions",
        }:
            raise TranscriptDecodeError("Unsupported permission_mode")
        return SessionStartedRecord(
            _string(data, "session_id"),
            _timestamp_string(data, "created_at"),
            cwd,
            provider_id,
            _string(data, "model"),
            permission_mode,
            _optional_positive_int(data, "max_steps"),
            _positive_int(data, "max_output_tokens"),
            _positive_int(data, "context_chars"),
            _model_limits(data.get("model_limits")),
            _optional_non_empty_string(data, "model_limit_source"),
            _optional_positive_int(data, "compact_trigger_tokens"),
            _optional_non_empty_string(data, "provider_protocol"),
        )
    if kind == "session_metadata":
        return SessionMetadataRecord(
            _timestamp_string(data, "created_at"),
            _timestamp_string(data, "updated_at"),
            _optional_non_empty_string(data, "title"),
            _optional_non_empty_string(data, "last_prompt"),
        )
    if kind == "content_replacement":
        return ContentReplacementRecord(
            _string(data, "tool_use_id"),
            _string(data, "tool_name"),
            _positive_int(data, "original_chars"),
            _string(data, "content"),
        )
    if kind == "compact_boundary":
        trigger = _string(data, "trigger")
        actual_trigger: Literal["auto", "manual", "reactive"]
        if trigger == "auto":
            actual_trigger = "auto"
        elif trigger == "manual":
            actual_trigger = "manual"
        elif trigger == "reactive":
            actual_trigger = "reactive"
        else:
            raise TranscriptDecodeError("Unsupported compact trigger")
        return CompactBoundaryRecord(
            _string(data, "id"),
            _string(data, "parent_uuid"),
            _string(data, "summary_uuid"),
            actual_trigger,
            _positive_int(data, "pre_compact_chars"),
        )
    if kind == "tool_presentation":
        return ToolPresentationRecord(
            _string(data, "tool_use_id"),
            _tool_presentation(data.get("presentation")),
        )
    if kind == "provider_replay":
        continuation = _optional_continuation(data)
        if continuation is None:
            raise TranscriptDecodeError("provider replay continuation is required")
        return ProviderReplaySidecarRecord(
            _string(data, "entry_id"),
            _string(data, "content_id"),
            continuation,
        )
    uuid = _string(data, "uuid")
    parent_uuid = _optional_string(data, "parent_uuid")
    timestamp = _string(data, "timestamp")
    if kind == "human_message":
        return HumanMessageRecord(
            uuid,
            parent_uuid,
            timestamp,
            _string(data, "content"),
        )
    if kind == "conversation_summary_message":
        return ConversationSummaryMessageRecord(
            uuid,
            parent_uuid,
            timestamp,
            _string(data, "content"),
        )
    if kind == "attachment_message":
        try:
            payload = to_json_object(data.get("payload"))
            _attachment_from_json(payload)
        except (TypeError, ValueError) as error:
            raise TranscriptDecodeError(str(error)) from error
        return AttachmentMessageRecord(uuid, parent_uuid, timestamp, payload)
    if kind == "assistant_message":
        raw = _list(data, "content")
        assistant_content = tuple(_assistant_block(item) for item in raw)
        if not assistant_content:
            raise TranscriptDecodeError("Assistant content must not be empty")
        return AssistantMessageRecord(
            uuid,
            parent_uuid,
            timestamp,
            assistant_content,
            _usage(data.get("usage")),
            _optional_binding(data.get("provider_binding")),
            (
                _positive_int(data, "request_input_tokens_estimate")
                if "request_input_tokens_estimate" in data
                else None
            ),
        )
    if kind == "tool_results_message":
        raw = _list(data, "content")
        result_content = tuple(_result(item) for item in raw)
        if not result_content:
            raise TranscriptDecodeError("Tool results must not be empty")
        return LegacyToolResultBatchRecord(
            uuid,
            parent_uuid,
            timestamp,
            result_content,
            _string(data, "source_assistant_uuid"),
        )
    if kind == "tool_result_batch":
        raw = _list(data, "content")
        result_content = tuple(_result(item) for item in raw)
        if not result_content:
            raise TranscriptDecodeError("Tool result batch must not be empty")
        return ToolResultBatchRecord(
            uuid,
            parent_uuid,
            timestamp,
            result_content,
            _string(data, "source_assistant_id"),
        )
    raise AssertionError("Validated transcript discriminator was not handled")


def _assistant_block_json(
    block: TextContentRecord | ToolCallRecord | ReasoningContentRecord,
) -> JsonObject:
    if isinstance(block, TextContentRecord):
        result: JsonObject = {"type": "text", "text": block.text}
        if block.continuation is not None:
            result["continuation"] = _continuation_json(block.continuation)
        return result
    if isinstance(block, ToolCallRecord):
        result = {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
        if block.continuation is not None:
            result["continuation"] = _continuation_json(block.continuation)
        return result
    result = {
        "type": "reasoning",
        "id": block.id,
        "presentation": {
            "disclosure": block.presentation.disclosure,
            "parts": list(block.presentation.parts),
        },
    }
    if block.continuation is not None:
        result["continuation"] = _continuation_json(block.continuation)
    return result


def _continuation_json(state: ProviderContinuationState) -> JsonObject:
    return {
        "binding": _binding_json(state.binding),
        "replay_scope": state.replay_scope,
        "payload": state.payload,
    }


def _binding_json(binding: ProviderBinding) -> JsonObject:
    return {
        "protocol": binding.protocol,
        "provider_id": binding.provider_id,
        "model": binding.model,
        "base_url": binding.base_url,
    }


def _result_json(block: ToolResultRecord) -> JsonObject:
    result: JsonObject = {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
    }
    if block.presentation is not None:
        result["presentation"] = {
            "summary": block.presentation.summary,
            "detail": block.presentation.detail,
            "truncated": block.presentation.truncated,
        }
    return result


def _usage_json(usage: TokenUsage) -> JsonObject:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "provider_reported": usage.provider_reported,
    }


def _skill_activation_json(payload: SkillActivationAttachment) -> JsonObject:
    return {
        "kind": payload.kind,
        "name": payload.name,
        "instructions": payload.instructions,
        "source": payload.source,
        "locator": payload.locator,
        "compatibility": payload.compatibility,
        "allowed_tools": list(payload.allowed_tools),
    }


def _attachment_to_json(payload: AttachmentPayload) -> JsonObject:
    if isinstance(payload, FileMentionAttachment):
        return {
            "kind": payload.kind,
            "path": payload.path,
            "body": payload.body,
            "is_directory": payload.is_directory,
        }
    if isinstance(payload, TodoReminderAttachment):
        return {"kind": payload.kind, "content": payload.content}
    if isinstance(payload, BackgroundTaskCompletionAttachment):
        return {
            "kind": payload.kind,
            "owner_run_id": payload.owner_run_id,
            "task_id": payload.task_id,
            "result": payload.result,
        }
    if isinstance(payload, SkillListingAttachment):
        return {
            "kind": payload.kind,
            "catalog_version": payload.catalog_version,
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                }
                for skill in payload.skills
            ],
        }
    if isinstance(payload, SkillActivationAttachment):
        return _skill_activation_json(payload)
    if isinstance(payload, ToolDiscoveryAttachment):
        return {
            "kind": payload.kind,
            "mode": payload.mode,
            "definitions": [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.input_schema,
                    "fingerprint": item.fingerprint,
                }
                for item in payload.definitions
            ],
        }
    if isinstance(payload, ToolDiscoveryInvalidationAttachment):
        return {"kind": payload.kind, "names": list(payload.names)}
    if isinstance(payload, ToolSearchListingAttachment):
        return {"kind": payload.kind, "names": list(payload.names)}
    return {
        "kind": payload.kind,
        "skills": [_skill_activation_json(skill) for skill in payload.skills],
    }


def _attachment_from_json(value: object) -> AttachmentPayload:
    data = _object(value)
    kind = _string(data, "kind")
    try:
        if kind == "file_mention":
            _require_exact_fields(
                data, frozenset({"kind", "path", "body", "is_directory"})
            )
            is_directory = data.get("is_directory")
            if not isinstance(is_directory, bool):
                raise TranscriptDecodeError("is_directory must be boolean")
            return FileMentionAttachment(
                _string(data, "path"), _string(data, "body"), is_directory
            )
        if kind == "todo_reminder":
            _require_exact_fields(data, frozenset({"kind", "content"}))
            return TodoReminderAttachment(_string(data, "content"))
        if kind == "background_task_completion":
            _require_exact_fields(
                data,
                frozenset({"kind", "owner_run_id", "task_id", "result"}),
            )
            return BackgroundTaskCompletionAttachment(
                _string(data, "owner_run_id"),
                _string(data, "task_id"),
                to_json_object(data.get("result")),
            )
        if kind == "skill_listing":
            _require_exact_fields(
                data, frozenset({"kind", "catalog_version", "skills"})
            )
            version = data.get("catalog_version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                raise TranscriptDecodeError("catalog_version must not be negative")
            return SkillListingAttachment(
                version,
                tuple(_skill_listing_entry(item) for item in _list(data, "skills")),
            )
        if kind == "skill_activation":
            return _skill_activation(data)
        if kind == "invoked_skills":
            _require_exact_fields(data, frozenset({"kind", "skills"}))
            return InvokedSkillsAttachment(
                tuple(
                    _skill_activation(_object(item)) for item in _list(data, "skills")
                )
            )
        if kind == "tool_discovery":
            _require_exact_fields(data, frozenset({"kind", "mode", "definitions"}))
            mode = _string(data, "mode")
            if mode not in {"dispatcher", "native"}:
                raise TranscriptDecodeError("Invalid tool discovery mode")
            actual_mode: Literal["dispatcher", "native"] = (
                "dispatcher" if mode == "dispatcher" else "native"
            )
            return ToolDiscoveryAttachment(
                tuple(
                    _tool_discovery_definition(_object(item))
                    for item in _list(data, "definitions")
                ),
                actual_mode,
            )
        if kind == "tool_discovery_invalidation":
            _require_exact_fields(data, frozenset({"kind", "names"}))
            return ToolDiscoveryInvalidationAttachment(
                tuple(
                    _non_empty_string_item(item, "names")
                    for item in _list(data, "names")
                )
            )
        if kind == "tool_search_listing":
            _require_exact_fields(data, frozenset({"kind", "names"}))
            return ToolSearchListingAttachment(
                tuple(
                    _non_empty_string_item(item, "names")
                    for item in _list(data, "names")
                )
            )
    except (TypeError, ValueError) as error:
        if isinstance(error, TranscriptDecodeError):
            raise
        raise TranscriptDecodeError(str(error)) from error
    raise TranscriptDecodeError(f"Unsupported attachment payload: {kind}")


def _skill_listing_entry(value: object) -> SkillListingEntry:
    data = _object(value)
    _require_exact_fields(data, frozenset({"name", "description", "source"}))
    return SkillListingEntry(
        _string(data, "name"),
        _string(data, "description"),
        _string(data, "source"),
    )


def _tool_discovery_definition(
    data: Mapping[str, object],
) -> ToolDiscoveryDefinition:
    _require_exact_fields(
        data,
        frozenset({"name", "description", "input_schema", "fingerprint"}),
    )
    return ToolDiscoveryDefinition(
        _string(data, "name"),
        _possibly_empty_string(data, "description"),
        to_json_object(data.get("input_schema")),
        _string(data, "fingerprint"),
    )


def _non_empty_string_item(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptDecodeError(f"{field} must contain non-empty strings")
    return value


def _possibly_empty_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise TranscriptDecodeError(f"{key} must be a string")
    return value


def _skill_activation(data: Mapping[str, object]) -> SkillActivationAttachment:
    _require_exact_fields(
        data,
        frozenset(
            {
                "kind",
                "name",
                "instructions",
                "source",
                "locator",
                "compatibility",
                "allowed_tools",
            }
        ),
    )
    if _string(data, "kind") != "skill_activation":
        raise TranscriptDecodeError("Invoked Skill must contain skill_activation")
    allowed = _list(data, "allowed_tools")
    if not all(isinstance(item, str) and item for item in allowed):
        raise TranscriptDecodeError("allowed_tools must contain non-empty strings")
    return SkillActivationAttachment(
        _string(data, "name"),
        _string(data, "instructions"),
        _string(data, "source"),
        _string(data, "locator"),
        _optional_non_empty_string(data, "compatibility"),
        tuple(item for item in allowed if isinstance(item, str)),
    )


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise TranscriptDecodeError("Expected an object")
    return value


def _require_exact_fields(data: Mapping[str, object], expected: frozenset[str]) -> None:
    actual = frozenset(data)
    if actual != expected:
        unexpected = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise TranscriptDecodeError(
            f"Invalid fields; unexpected={unexpected}, missing={missing}"
        )


def _string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise TranscriptDecodeError(f"{key!r} must be a non-empty string")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise TranscriptDecodeError(f"{key!r} must be a string or null")
    return value


def _optional_non_empty_string(data: Mapping[str, object], key: str) -> str | None:
    value = _optional_string(data, key)
    if value is not None and not value.strip():
        raise TranscriptDecodeError(f"{key!r} must be non-empty or null")
    return value


def _timestamp_string(data: Mapping[str, object], key: str) -> str:
    value = _string(data, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise TranscriptDecodeError(f"{key!r} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise TranscriptDecodeError(f"{key!r} must include a timezone")
    return value


def _list(data: Mapping[str, object], key: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise TranscriptDecodeError(f"{key!r} must be a list")
    return value


def _positive_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TranscriptDecodeError(f"{key!r} must be positive")
    return value


def _optional_positive_int(data: Mapping[str, object], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TranscriptDecodeError(f"{key!r} must be positive or null")
    return value


def _model_limits(value: object) -> ModelLimits:
    if value is None:
        return ModelLimits()
    raw = _object(value)
    _require_exact_fields(
        raw,
        frozenset({"context_window_tokens", "max_input_tokens", "max_output_tokens"}),
    )
    return ModelLimits(
        _optional_positive_int(raw, "context_window_tokens"),
        _optional_positive_int(raw, "max_input_tokens"),
        _optional_positive_int(raw, "max_output_tokens"),
    )


def _assistant_block(
    value: object,
) -> TextContentRecord | ToolCallRecord | ReasoningContentRecord:
    data = _object(value)
    kind = _string(data, "type")
    if kind == "text":
        expected = {"type", "text"}
        if "continuation" in data:
            expected.add("continuation")
        _require_exact_fields(data, frozenset(expected))
        return TextContentRecord(_string(data, "text"), _optional_continuation(data))
    if kind == "tool_call":
        expected = {"type", "id", "name", "input"}
        if "continuation" in data:
            expected.add("continuation")
        _require_exact_fields(data, frozenset(expected))
        try:
            input_ = to_json_object(data.get("input"))
        except TypeError as error:
            raise TranscriptDecodeError("Tool input must be an object") from error
        return ToolCallRecord(
            _string(data, "id"),
            _string(data, "name"),
            input_,
            _optional_continuation(data),
        )
    if kind == "reasoning":
        expected = {"type", "id", "presentation"}
        if "continuation" in data:
            expected.add("continuation")
        _require_exact_fields(data, frozenset(expected))
        try:
            raw_presentation = _object(data.get("presentation"))
            _require_exact_fields(raw_presentation, frozenset({"disclosure", "parts"}))
            parts = _list(raw_presentation, "parts")
            if not all(isinstance(part, str) for part in parts):
                raise TranscriptDecodeError("reasoning parts must be strings")
            presentation = ReasoningPresentation(
                _string(raw_presentation, "disclosure"),  # type: ignore[arg-type]
                tuple(part for part in parts if isinstance(part, str)),
            )
            continuation = _optional_continuation(data)
        except (TypeError, ValueError) as error:
            raise TranscriptDecodeError(str(error)) from error
        return ReasoningContentRecord(_string(data, "id"), presentation, continuation)
    raise TranscriptDecodeError(f"Unsupported assistant content: {kind}")


def _optional_continuation(
    data: Mapping[str, object],
) -> ProviderContinuationState | None:
    raw = data.get("continuation")
    if raw is None:
        return None
    item = _object(raw)
    _require_exact_fields(item, frozenset({"binding", "replay_scope", "payload"}))
    try:
        binding = _optional_binding(item.get("binding"))
        if binding is None:
            raise TranscriptDecodeError("continuation binding is required")
        return ProviderContinuationState(
            binding,
            _string(item, "replay_scope"),  # type: ignore[arg-type]
            to_json_object(item.get("payload")),
        )
    except (TypeError, ValueError) as error:
        raise TranscriptDecodeError(str(error)) from error


def _optional_binding(value: object) -> ProviderBinding | None:
    if value is None:
        return None
    binding_raw = _object(value)
    _require_exact_fields(
        binding_raw, frozenset({"protocol", "provider_id", "model", "base_url"})
    )
    base_url = binding_raw.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise TranscriptDecodeError("binding base_url must be string or null")
    try:
        return ProviderBinding(
            _string(binding_raw, "protocol"),
            _string(binding_raw, "provider_id"),
            _string(binding_raw, "model"),
            base_url,
        )
    except ValueError as error:
        raise TranscriptDecodeError(str(error)) from error


def _result(value: object) -> ToolResultRecord:
    data = _object(value)
    if _string(data, "type") != "tool_result":
        raise TranscriptDecodeError("Unsupported tool result content")
    expected = {"type", "tool_use_id", "content", "is_error"}
    if data.get("presentation") is not None:
        expected.add("presentation")
    _require_exact_fields(data, frozenset(expected))
    error = data.get("is_error", False)
    if not isinstance(error, bool):
        raise TranscriptDecodeError("is_error must be boolean")
    presentation = None
    raw = data.get("presentation")
    if raw is not None:
        item = _object(raw)
        _require_exact_fields(item, frozenset({"summary", "detail", "truncated"}))
        detail = item.get("detail")
        truncated = item.get("truncated", False)
        if detail is not None and not isinstance(detail, str):
            raise TranscriptDecodeError("presentation detail must be string or null")
        if not isinstance(truncated, bool):
            raise TranscriptDecodeError("presentation truncated must be boolean")
        presentation = ToolResultPresentation(
            _string(item, "summary"), detail, truncated
        )
    return ToolResultRecord(
        _string(data, "tool_use_id"), _string(data, "content"), error, presentation
    )


def _tool_presentation(value: object) -> ToolResultPresentation:
    item = _object(value)
    _require_exact_fields(item, frozenset({"summary", "detail", "truncated"}))
    detail = item.get("detail")
    truncated = item.get("truncated")
    if detail is not None and not isinstance(detail, str):
        raise TranscriptDecodeError("presentation detail must be string or null")
    if not isinstance(truncated, bool):
        raise TranscriptDecodeError("presentation truncated must be boolean")
    return ToolResultPresentation(_string(item, "summary"), detail, truncated)


def _usage(value: object) -> TokenUsage:
    data = _object(value)
    expected = {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    if "provider_reported" in data:
        expected.add("provider_reported")
    _require_exact_fields(data, frozenset(expected))
    values = []
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        raw = data.get(key)
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise TranscriptDecodeError("Usage counts must be integers")
        values.append(raw)
    try:
        reported = data.get("provider_reported", False)
        if not isinstance(reported, bool):
            raise TranscriptDecodeError("provider_reported must be boolean")
        return TokenUsage(values[0], values[1], values[2], values[3], reported)
    except ValueError as error:
        raise TranscriptDecodeError(str(error)) from error


_MESSAGE_COMMON = frozenset(
    {"type", "schema_version", "uuid", "parent_uuid", "timestamp", "content"}
)
_ATTACHMENT_MESSAGE_FIELDS = frozenset(
    {"type", "schema_version", "uuid", "parent_uuid", "timestamp", "payload"}
)
_ENTRY_FIELDS: dict[str, frozenset[str]] = {
    "session_started": frozenset(
        {
            "type",
            "schema_version",
            "session_id",
            "created_at",
            "cwd",
            "provider_id",
            "model",
            "permission_mode",
            "max_steps",
            "max_output_tokens",
            "context_chars",
        }
    ),
    "session_metadata": frozenset(
        {
            "type",
            "schema_version",
            "created_at",
            "updated_at",
            "title",
            "last_prompt",
        }
    ),
    "human_message": _MESSAGE_COMMON,
    "assistant_message": _MESSAGE_COMMON | {"usage"},
    "tool_results_message": _MESSAGE_COMMON | {"source_assistant_uuid"},
    "tool_result_batch": _MESSAGE_COMMON | {"source_assistant_id"},
    "conversation_summary_message": _MESSAGE_COMMON,
    "attachment_message": _ATTACHMENT_MESSAGE_FIELDS,
    "content_replacement": frozenset(
        {
            "type",
            "schema_version",
            "tool_use_id",
            "tool_name",
            "original_chars",
            "content",
        }
    ),
    "compact_boundary": frozenset(
        {
            "type",
            "schema_version",
            "id",
            "parent_uuid",
            "summary_uuid",
            "trigger",
            "pre_compact_chars",
        }
    ),
    "tool_presentation": frozenset(
        {"type", "schema_version", "tool_use_id", "presentation"}
    ),
    "provider_replay": frozenset(
        {"type", "schema_version", "entry_id", "content_id", "continuation"}
    ),
}
