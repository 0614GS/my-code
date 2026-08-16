"""仅供 sessions adapter 使用的 JSONL 持久化类型。"""

from dataclasses import dataclass
from typing import Literal

from nano_code.messages.primitives import JsonObject, TokenUsage
from nano_code.presentation import ToolResultPresentation


@dataclass(frozen=True, slots=True)
class TextContentRecord:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    id: str
    name: str
    input: JsonObject
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True, slots=True)
class ToolResultRecord:
    tool_use_id: str
    content: str
    is_error: bool = False
    presentation: ToolResultPresentation | None = None
    type: Literal["tool_result"] = "tool_result"


@dataclass(frozen=True, slots=True)
class HumanMessageRecord:
    uuid: str
    parent_uuid: str | None
    timestamp: str
    content: str
    type: Literal["human_message"] = "human_message"
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class AssistantMessageRecord:
    uuid: str
    parent_uuid: str | None
    timestamp: str
    content: tuple[TextContentRecord | ToolCallRecord, ...]
    usage: TokenUsage
    type: Literal["assistant_message"] = "assistant_message"
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class ToolResultsMessageRecord:
    uuid: str
    parent_uuid: str | None
    timestamp: str
    content: tuple[ToolResultRecord, ...]
    source_assistant_uuid: str
    type: Literal["tool_results_message"] = "tool_results_message"
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class ConversationSummaryMessageRecord:
    uuid: str
    parent_uuid: str | None
    timestamp: str
    content: str
    type: Literal["conversation_summary_message"] = "conversation_summary_message"
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class ContentReplacementRecord:
    tool_use_id: str
    tool_name: str
    original_chars: int
    content: str
    type: Literal["content_replacement"] = "content_replacement"
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class CompactBoundaryRecord:
    id: str
    parent_uuid: str
    summary_uuid: str
    trigger: Literal["auto", "manual", "reactive"]
    pre_compact_chars: int
    type: Literal["compact_boundary"] = "compact_boundary"
    schema_version: Literal[1] = 1


type MessageRecord = (
    HumanMessageRecord
    | AssistantMessageRecord
    | ToolResultsMessageRecord
    | ConversationSummaryMessageRecord
)
type TranscriptEntry = MessageRecord | ContentReplacementRecord | CompactBoundaryRecord
