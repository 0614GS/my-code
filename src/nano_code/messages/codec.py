"""会话消息的严格 JSON 编解码。"""

from collections.abc import Mapping

from nano_code.messages.models import (
    ContentBlock,
    JsonObject,
    JsonValue,
    MessageOrigin,
    MessageRole,
    SystemContextBlock,
    SystemContextKind,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
    to_json_object,
)
from nano_code.presentation import ToolResultPresentation


class MessageDecodeError(ValueError):
    """会话记录不符合消息 schema 时抛出。"""


def block_to_json(block: ContentBlock) -> JsonObject:
    """编码不含 provider 专属字段的内容块。"""

    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, SystemContextBlock):
        return {
            "type": "system_context",
            "kind": block.kind,
            "content": block.content,
        }
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
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


def message_to_json(message: TranscriptMessage) -> JsonObject:
    """编码一条内部消息用于 JSONL 持久化。"""

    # 磁盘记录版本独立于 provider SDK 类型，使会话迁移无需修改智能体内部数据类。
    result: JsonObject = {
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
    if message.usage is not None:
        result["usage"] = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_creation_input_tokens": (message.usage.cache_creation_input_tokens),
            "cache_read_input_tokens": message.usage.cache_read_input_tokens,
        }
    return result


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
    """解码并校验一个内部内容块。"""

    if not isinstance(value, dict):
        raise MessageDecodeError("Content block must be an object")
    data: Mapping[str, object] = value
    block_type = _required_string(data, "type")
    if block_type == "text":
        return TextBlock(text=_required_string(data, "text"))
    if block_type == "system_context":
        raw_kind = _required_string(data, "kind")
        if raw_kind == "system_reminder":
            kind: SystemContextKind = "system_reminder"
        elif raw_kind == "conversation_summary":
            kind = "conversation_summary"
        else:
            raise MessageDecodeError(f"Unsupported system context kind: {raw_kind}")
        return SystemContextBlock(
            kind=kind,
            content=_required_string(data, "content"),
        )
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
            presentation=_presentation_from_json(data.get("presentation")),
        )
    raise MessageDecodeError(f"Unsupported content block type: {block_type}")


def _presentation_from_json(value: object) -> ToolResultPresentation | None:
    """解析可选展示快照；旧版 Transcript 中不存在该字段。"""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise MessageDecodeError("Tool presentation must be an object")
    data: Mapping[str, object] = value
    detail = data.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise MessageDecodeError("Tool presentation detail must be a string or null")
    truncated = data.get("truncated", False)
    if not isinstance(truncated, bool):
        raise MessageDecodeError("Tool presentation truncated must be a boolean")
    return ToolResultPresentation(
        summary=_required_string(data, "summary"),
        detail=detail,
        truncated=truncated,
    )


def _usage_from_json(value: object) -> TokenUsage | None:
    """解析可选 provider usage；旧会话没有该字段。"""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise MessageDecodeError("Message usage must be an object")
    data: Mapping[str, object] = value
    input_tokens = data.get("input_tokens")
    output_tokens = data.get("output_tokens")
    cache_creation = data.get("cache_creation_input_tokens", 0)
    cache_read = data.get("cache_read_input_tokens", 0)
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or not isinstance(cache_creation, int)
        or isinstance(cache_creation, bool)
        or not isinstance(cache_read, int)
        or isinstance(cache_read, bool)
    ):
        raise MessageDecodeError("Message usage token counts must be integers")
    try:
        return TokenUsage(input_tokens, output_tokens, cache_creation, cache_read)
    except ValueError as error:
        raise MessageDecodeError(str(error)) from error


def message_from_json(value: object) -> TranscriptMessage:
    """解码并校验一条带版本的会话记录。"""

    # 恢复时会话记录属于不可信输入：它可能过时、不完整或被编辑过。
    # 构造强类型消息前，先重新进入严格 JSON 数据域。
    try:
        data = to_json_object(value)
    except TypeError as error:
        raise MessageDecodeError("Transcript record must be a JSON object") from error

    if data.get("type") != "message" or data.get("version") != 1:
        raise MessageDecodeError("Unsupported transcript record")

    raw_content: JsonValue = data.get("content")
    if not isinstance(raw_content, list):
        raise MessageDecodeError("'content' must be a list")

    # 显式列出每个 Literal 分支，使运行时校验与 mypy 类型收窄保持一致；
    # 宽泛的 cast 会让未来的未知值渗入核心层。
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

    return TranscriptMessage(
        uuid=_required_string(data, "uuid"),
        parent_uuid=_optional_string(data, "parent_uuid"),
        timestamp=_required_string(data, "timestamp"),
        role=role,
        origin=origin,
        source_message_uuid=_optional_string(data, "source_message_uuid"),
        usage=_usage_from_json(data.get("usage")),
        content=tuple(block_from_json(block) for block in raw_content),
    )
