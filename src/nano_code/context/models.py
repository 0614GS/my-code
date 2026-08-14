"""上下文规划阶段使用的不可变值对象。"""

from dataclasses import dataclass, field
from enum import StrEnum

from nano_code.messages import ChatMessage, ContentBlock, MessageRole
from nano_code.tools.base import ToolDefinition


class PromptStability(StrEnum):
    """提示词片段在多次请求之间的预期稳定范围。"""

    STATIC = "static"
    SESSION = "session"
    TURN = "turn"


@dataclass(frozen=True, slots=True)
class ContentReplacement:
    """按 tool ID 冻结的一次模型可见内容替换。"""

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
        if original_chars < 1:
            raise ValueError("original_chars must be positive")
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
class PromptSection:
    """具有稳定身份和缓存语义的一段 system prompt。"""

    key: str
    content: str
    stability: PromptStability

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Prompt section key must not be empty")
        if not self.content.strip():
            raise ValueError("Prompt section content must not be empty")


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """ContextPlanner 某一时刻读取的会话工作集快照。"""

    messages: tuple[ChatMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """仅包含模型协议所需字段，不携带 Transcript 本地元数据。"""

    role: MessageRole
    content: tuple[ContentBlock, ...]


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """一次请求各组成部分的可观察预算报告。"""

    message_limit_chars: int
    message_chars: int
    system_chars: int
    tool_schema_chars: int
    reserved_output_tokens: int
    last_actual_input_tokens: int | None
    incremental_tokens: int
    estimated_input_tokens: int

    @property
    def estimated_input_chars(self) -> int:
        return self.message_chars + self.system_chars + self.tool_schema_chars

    @property
    def estimated_total_tokens(self) -> int:
        return self.estimated_input_tokens + self.reserved_output_tokens


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """经过上下文策略处理、可交给任意 provider 的请求计划。"""

    system_prompt: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    max_output_tokens: int
    prompt_sections: tuple[PromptSection, ...] = field(default_factory=tuple)
    budget: ContextBudget | None = None
    new_content_replacements: tuple[ContentReplacement, ...] = field(
        default_factory=tuple
    )
