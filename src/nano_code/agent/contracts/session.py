"""会话事实、工作集和 compact 持久化边界。"""

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from nano_code.messages import ChatMessage

CompactTrigger = Literal["auto", "manual", "reactive"]


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
class ConversationSnapshot:
    """ContextPort 某一时刻读取的会话工作集快照。"""

    messages: tuple[ChatMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CompactBoundary:
    """连接完整 Transcript 与压缩后工作集的持久化边界。"""

    parent_uuid: str
    summary_uuid: str
    trigger: CompactTrigger
    pre_compact_chars: int
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if self.pre_compact_chars < 1:
            raise ValueError("pre_compact_chars must be positive")


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """一次读取会话事实及其当前工作集所需的完整快照。"""

    history: tuple[ChatMessage, ...]
    working_set: tuple[ChatMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = field(default_factory=tuple)
    compact_boundaries: tuple[CompactBoundary, ...] = field(default_factory=tuple)

    @property
    def full_history(self) -> tuple[ChatMessage, ...]:
        return self.history

    @property
    def all_messages(self) -> tuple[ChatMessage, ...]:
        return self.history

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return self.history

    @property
    def working_messages(self) -> tuple[ChatMessage, ...]:
        return self.working_set

    @property
    def replacements(self) -> tuple[ContentReplacement, ...]:
        return self.content_replacements

    @property
    def boundaries(self) -> tuple[CompactBoundary, ...]:
        return self.compact_boundaries

    @property
    def compact_boundary(self) -> CompactBoundary | None:
        return self.compact_boundaries[-1] if self.compact_boundaries else None
