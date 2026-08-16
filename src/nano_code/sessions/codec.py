"""TranscriptEntry 的严格 JSON codec 及领域消息映射。"""

from collections.abc import Mapping
from typing import Literal

from nano_code.agent.contracts.session import CompactBoundary, ContentReplacement
from nano_code.messages import (
    AssistantMessage,
    ConversationMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    to_json_object,
)
from nano_code.messages.primitives import JsonObject
from nano_code.presentation import ToolResultPresentation
from nano_code.sessions.records import (
    AssistantMessageRecord,
    CompactBoundaryRecord,
    ContentReplacementRecord,
    ConversationSummaryMessageRecord,
    HumanMessageRecord,
    MessageRecord,
    TextContentRecord,
    ToolCallRecord,
    ToolResultRecord,
    ToolResultsMessageRecord,
    TranscriptEntry,
)


class TranscriptDecodeError(ValueError):
    pass


type DecodedEntry = ConversationMessage | ContentReplacement | CompactBoundary


def decode_entry(value: object) -> DecodedEntry:
    """将不可信 JSON 一步映射为 sessions port 可见的领域值。"""

    entry = entry_from_json(value)
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
    return record_to_message(entry)


def encode_message(message: ConversationMessage) -> JsonObject:
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


def message_to_record(message: ConversationMessage) -> MessageRecord:
    if isinstance(message, HumanMessage):
        return HumanMessageRecord(
            message.uuid, message.parent_uuid, message.timestamp, message.content
        )
    if isinstance(message, AssistantMessage):
        assistant_content: tuple[TextContentRecord | ToolCallRecord, ...] = tuple(
            TextContentRecord(b.text)
            if isinstance(b, TextContent)
            else ToolCallRecord(b.id, b.name, b.input)
            for b in message.content
        )
        return AssistantMessageRecord(
            message.uuid,
            message.parent_uuid,
            message.timestamp,
            assistant_content,
            message.usage,
        )
    if isinstance(message, ToolResultsMessage):
        result_content: tuple[ToolResultRecord, ...] = tuple(
            ToolResultRecord(b.tool_use_id, b.content, b.is_error, b.presentation)
            for b in message.content
        )
        return ToolResultsMessageRecord(
            message.uuid,
            message.parent_uuid,
            message.timestamp,
            result_content,
            message.source_assistant_uuid,
        )
    return ConversationSummaryMessageRecord(
        message.uuid, message.parent_uuid, message.timestamp, message.content
    )


def record_to_message(record: MessageRecord) -> ConversationMessage:
    if isinstance(record, HumanMessageRecord):
        return HumanMessage(
            record.content, record.uuid, record.parent_uuid, record.timestamp
        )
    if isinstance(record, AssistantMessageRecord):
        assistant_content: tuple[TextContent | ToolCall, ...] = tuple(
            TextContent(b.text)
            if isinstance(b, TextContentRecord)
            else ToolCall(b.id, b.name, b.input)
            for b in record.content
        )
        return AssistantMessage(
            assistant_content,
            record.usage,
            record.uuid,
            record.parent_uuid,
            record.timestamp,
        )
    if isinstance(record, ToolResultsMessageRecord):
        result_content: tuple[ToolResult, ...] = tuple(
            ToolResult(b.tool_use_id, b.content, b.is_error, b.presentation)
            for b in record.content
        )
        return ToolResultsMessage(
            result_content,
            record.source_assistant_uuid,
            record.uuid,
            record.parent_uuid,
            record.timestamp,
        )
    return ConversationSummaryMessage(
        record.content, record.uuid, record.parent_uuid, record.timestamp
    )


def entry_to_json(entry: TranscriptEntry) -> JsonObject:
    base: JsonObject = {"type": entry.type, "schema_version": entry.schema_version}
    if isinstance(
        entry,
        (
            HumanMessageRecord,
            AssistantMessageRecord,
            ToolResultsMessageRecord,
            ConversationSummaryMessageRecord,
        ),
    ):
        base.update(
            uuid=entry.uuid, parent_uuid=entry.parent_uuid, timestamp=entry.timestamp
        )
    if isinstance(entry, HumanMessageRecord | ConversationSummaryMessageRecord):
        base["content"] = entry.content
    elif isinstance(entry, AssistantMessageRecord):
        base["content"] = [_assistant_block_json(b) for b in entry.content]
        base["usage"] = _usage_json(entry.usage)
    elif isinstance(entry, ToolResultsMessageRecord):
        base["content"] = [_result_json(b) for b in entry.content]
        base["source_assistant_uuid"] = entry.source_assistant_uuid
    elif isinstance(entry, ContentReplacementRecord):
        base.update(
            tool_use_id=entry.tool_use_id,
            tool_name=entry.tool_name,
            original_chars=entry.original_chars,
            content=entry.content,
        )
    else:
        base.update(
            id=entry.id,
            parent_uuid=entry.parent_uuid,
            summary_uuid=entry.summary_uuid,
            trigger=entry.trigger,
            pre_compact_chars=entry.pre_compact_chars,
        )
    return base


def entry_from_json(value: object) -> TranscriptEntry:
    try:
        data = to_json_object(value)
    except TypeError as error:
        raise TranscriptDecodeError("Transcript entry must be an object") from error
    if data.get("schema_version") != 1:
        raise TranscriptDecodeError("Unsupported transcript schema version")
    kind = _string(data, "type")
    expected_fields = _ENTRY_FIELDS.get(kind)
    if expected_fields is None:
        raise TranscriptDecodeError(f"Unsupported transcript entry type: {kind}")
    _require_exact_fields(data, expected_fields)
    if kind in {
        "human_message",
        "assistant_message",
        "tool_results_message",
        "conversation_summary_message",
    }:
        common = (
            _string(data, "uuid"),
            _optional_string(data, "parent_uuid"),
            _string(data, "timestamp"),
        )
    if kind == "human_message":
        return HumanMessageRecord(*common, _string(data, "content"))
    if kind == "conversation_summary_message":
        return ConversationSummaryMessageRecord(*common, _string(data, "content"))
    if kind == "assistant_message":
        raw = _list(data, "content")
        assistant_content = tuple(_assistant_block(item) for item in raw)
        if not assistant_content:
            raise TranscriptDecodeError("Assistant content must not be empty")
        return AssistantMessageRecord(
            *common, assistant_content, _usage(data.get("usage"))
        )
    if kind == "tool_results_message":
        raw = _list(data, "content")
        result_content = tuple(_result(item) for item in raw)
        if not result_content:
            raise TranscriptDecodeError("Tool results must not be empty")
        return ToolResultsMessageRecord(
            *common, result_content, _string(data, "source_assistant_uuid")
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
    raise AssertionError("Validated transcript discriminator was not handled")


def _assistant_block_json(block: TextContentRecord | ToolCallRecord) -> JsonObject:
    return (
        {"type": "text", "text": block.text}
        if isinstance(block, TextContentRecord)
        else {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    )


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
    }


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


def _assistant_block(value: object) -> TextContentRecord | ToolCallRecord:
    data = _object(value)
    kind = _string(data, "type")
    if kind == "text":
        _require_exact_fields(data, frozenset({"type", "text"}))
        return TextContentRecord(_string(data, "text"))
    if kind == "tool_call":
        _require_exact_fields(data, frozenset({"type", "id", "name", "input"}))
        try:
            input_ = to_json_object(data.get("input"))
        except TypeError as error:
            raise TranscriptDecodeError("Tool input must be an object") from error
        return ToolCallRecord(_string(data, "id"), _string(data, "name"), input_)
    raise TranscriptDecodeError(f"Unsupported assistant content: {kind}")


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


def _usage(value: object) -> TokenUsage:
    data = _object(value)
    _require_exact_fields(
        data,
        frozenset(
            {
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            }
        ),
    )
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
        return TokenUsage(*values)
    except ValueError as error:
        raise TranscriptDecodeError(str(error)) from error


_MESSAGE_COMMON = frozenset(
    {"type", "schema_version", "uuid", "parent_uuid", "timestamp", "content"}
)
_ENTRY_FIELDS: dict[str, frozenset[str]] = {
    "human_message": _MESSAGE_COMMON,
    "assistant_message": _MESSAGE_COMMON | {"usage"},
    "tool_results_message": _MESSAGE_COMMON | {"source_assistant_uuid"},
    "conversation_summary_message": _MESSAGE_COMMON,
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
}
