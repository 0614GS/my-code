"""Strict JSON encoding for transcript messages."""

from collections.abc import Mapping

from nano_code.messages.models import (
    ChatMessage,
    ContentBlock,
    JsonObject,
    JsonValue,
    MessageOrigin,
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    to_json_object,
)


class MessageDecodeError(ValueError):
    """Raised when a transcript record does not match the message schema."""


def block_to_json(block: ContentBlock) -> JsonObject:
    """Encode a content block without provider-specific fields."""

    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    return {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
    }


def message_to_json(message: ChatMessage) -> JsonObject:
    """Encode one internal message for JSONL persistence."""

    return {
        "type": "message",
        "version": 1,
        "uuid": message.uuid,
        "parent_uuid": message.parent_uuid,
        "timestamp": message.timestamp,
        "role": message.role,
        "origin": message.origin,
        "source_message_uuid": message.source_message_uuid,
        "content": [block_to_json(block) for block in message.content],
    }


def _required_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise MessageDecodeError(f"{key!r} must be a string")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MessageDecodeError(f"{key!r} must be a string or null")
    return value


def block_from_json(value: object) -> ContentBlock:
    """Decode and validate one internal content block."""

    if not isinstance(value, dict):
        raise MessageDecodeError("Content block must be an object")
    data: Mapping[str, object] = value
    block_type = _required_string(data, "type")
    if block_type == "text":
        return TextBlock(text=_required_string(data, "text"))
    if block_type == "tool_use":
        try:
            tool_input = to_json_object(data.get("input"))
        except TypeError as error:
            raise MessageDecodeError("Tool input must be a JSON object") from error
        return ToolUseBlock(
            id=_required_string(data, "id"),
            name=_required_string(data, "name"),
            input=tool_input,
        )
    if block_type == "tool_result":
        is_error = data.get("is_error", False)
        if not isinstance(is_error, bool):
            raise MessageDecodeError("'is_error' must be a boolean")
        return ToolResultBlock(
            tool_use_id=_required_string(data, "tool_use_id"),
            content=_required_string(data, "content"),
            is_error=is_error,
        )
    raise MessageDecodeError(f"Unsupported content block type: {block_type}")


def message_from_json(value: object) -> ChatMessage:
    """Decode and validate one versioned transcript record."""

    try:
        data = to_json_object(value)
    except TypeError as error:
        raise MessageDecodeError("Transcript record must be a JSON object") from error

    if data.get("type") != "message" or data.get("version") != 1:
        raise MessageDecodeError("Unsupported transcript record")

    raw_content: JsonValue = data.get("content")
    if not isinstance(raw_content, list):
        raise MessageDecodeError("'content' must be a list")

    raw_role = _required_string(data, "role")
    if raw_role == "user":
        role: MessageRole = "user"
    elif raw_role == "assistant":
        role = "assistant"
    else:
        raise MessageDecodeError(f"Unsupported role: {raw_role}")

    raw_origin = _required_string(data, "origin")
    if raw_origin == "human":
        origin: MessageOrigin = "human"
    elif raw_origin == "model":
        origin = "model"
    elif raw_origin == "tool":
        origin = "tool"
    elif raw_origin == "system":
        origin = "system"
    else:
        raise MessageDecodeError(f"Unsupported origin: {raw_origin}")

    return ChatMessage(
        uuid=_required_string(data, "uuid"),
        parent_uuid=_optional_string(data, "parent_uuid"),
        timestamp=_required_string(data, "timestamp"),
        role=role,
        origin=origin,
        source_message_uuid=_optional_string(data, "source_message_uuid"),
        content=tuple(block_from_json(block) for block in raw_content),
    )
