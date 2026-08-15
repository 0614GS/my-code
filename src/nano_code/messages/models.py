"""智能体核心使用的 provider 无关消息类型。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from nano_code.presentation import ToolResultPresentation

# 将跨层数据限制为 JSON，而不是允许 Any，使 provider payload、会话记录和
# 工具输入都能在每个边界接受检查。
type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]
type MessageRole = Literal["user", "assistant"]
type MessageOrigin = Literal["human", "model", "tool", "system"]
type SystemContextKind = Literal["system_reminder", "conversation_summary"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次模型请求的 provider token 用量。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_creation_input_tokens,
                self.cache_read_input_tokens,
            )
        ):
            raise ValueError("Token usage must not be negative")

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


def utc_now() -> str:
    """返回紧凑且包含时区的时间戳。"""

    return datetime.now(UTC).isoformat()


def new_id() -> str:
    """返回稳定的本地消息标识符。"""

    return str(uuid4())


def to_json_value(value: object) -> JsonValue:
    """校验任意值并将其复制到支持的 JSON 数据域。"""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = to_json_value(item)
        return result
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def to_json_object(value: object) -> JsonObject:
    """将任意值作为 JSON 对象进行校验。"""

    converted = to_json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("Expected a JSON object")
    return converted


@dataclass(frozen=True, slots=True)
class TextBlock:
    """用户或 assistant 文本块。"""

    text: str
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class SystemContextBlock:
    """由核心创建、在请求投影时才渲染为 XML 的上下文说明。"""

    kind: SystemContextKind
    content: str
    type: Literal["system_context"] = field(default="system_context", init=False)

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("System context content must not be empty")


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """模型对具名工具的调用请求。"""

    id: str
    name: str
    input: JsonObject
    # Literal 判别字段让 mypy 获得与 TypeScript 参考实现中标签联合类型相同的收窄能力。
    type: Literal["tool_use"] = field(default="tool_use", init=False)


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """与一个模型工具请求配对的结果。"""

    # tool_use_id 是 provider 协议标识，刻意与下方本地会话消息 UUID 分离。
    tool_use_id: str
    content: str
    is_error: bool = False
    presentation: ToolResultPresentation | None = None
    type: Literal["tool_result"] = field(default="tool_result", init=False)


type ContentBlock = TextBlock | SystemContextBlock | ToolUseBlock | ToolResultBlock
type AssistantBlock = TextBlock | ToolUseBlock


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """内部会话记录中的一条持久化消息。"""

    # role 是 provider 协议角色。origin 记录消息的实际创建方，因为工具结果也以
    # provider 的 user 角色传输。
    role: MessageRole
    content: tuple[ContentBlock, ...]
    origin: MessageOrigin

    # 本地 UUID 独立于 API ID 构成可恢复的会话记录链。
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    # 工具结果消息保留指向发起请求的 assistant 消息的直接来源边；
    # 这一点对并行调用尤为重要。
    source_message_uuid: str | None = None
    # 只有模型响应携带 usage。它是下一次请求预算的最近真实锚点，不能累加为
    # 当前上下文大小。
    usage: TokenUsage | None = None

    def __post_init__(self) -> None:
        # 构造时强制执行 provider 消息形状不变量，避免异常消息进入持久化后
        # 才在后续采样阶段失败。
        if not self.content:
            raise ValueError("A message must contain at least one content block")
        if self.role == "assistant" and any(
            isinstance(block, (SystemContextBlock, ToolResultBlock))
            for block in self.content
        ):
            raise ValueError("Assistant messages cannot contain system/tool results")
        if self.role == "user" and any(
            isinstance(block, ToolUseBlock) for block in self.content
        ):
            raise ValueError("User messages cannot contain tool uses")
        if self.origin == "tool" and not all(
            isinstance(block, ToolResultBlock) for block in self.content
        ):
            raise ValueError("Tool-origin messages may contain only tool results")
        if any(isinstance(block, SystemContextBlock) for block in self.content) and (
            self.role != "user" or self.origin != "system"
        ):
            raise ValueError(
                "System context blocks require a system-origin user message"
            )
        if self.usage is not None and (
            self.role != "assistant" or self.origin != "model"
        ):
            raise ValueError("Token usage is only valid on model assistant messages")

    @property
    def starts_human_turn(self) -> bool:
        """是否为真实用户提示，即是否可作为安全截断边界。"""

        return self.role == "user" and self.origin == "human"

    @property
    def starts_context_segment(self) -> bool:
        """是否可以作为模型工作集的语义起点。"""

        return self.role == "user" and self.origin in {"human", "system"}


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """与 provider 无关的 assistant 响应。"""

    content: tuple[AssistantBlock, ...]
    stop_reason: str
    usage: TokenUsage = field(default_factory=TokenUsage)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Model response contained no supported content blocks")
