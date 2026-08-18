"""会话事实、工作集和 compact 持久化边界。"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from nano_code.context.attachments.models import ContextAttachment
from nano_code.conversation import ConversationMessage
from nano_code.providers.catalog import ModelLimits

CompactTrigger = Literal["auto", "manual", "reactive"]


@dataclass(frozen=True, slots=True)
class SessionStart:
    session_id: str
    created_at: str
    cwd: str
    provider_id: str
    model: str
    permission_mode: str
    max_steps: int | None
    max_output_tokens: int
    context_chars: int
    model_limits: ModelLimits = ModelLimits()
    model_limit_source: str | None = None
    compact_trigger_tokens: int | None = None

    def __post_init__(self) -> None:
        try:
            parsed_id = UUID(self.session_id)
        except ValueError as error:
            raise ValueError("session_id must be a UUID") from error
        if str(parsed_id) != self.session_id.lower():
            raise ValueError("session_id must use canonical UUID syntax")
        _validate_session_timestamp(self.created_at, "created_at")
        if not Path(self.cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        if not self.provider_id or not self.model or not self.permission_mode:
            raise ValueError("Session start strings must not be empty")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be positive or null")
        if self.max_output_tokens < 1 or self.context_chars < 1:
            raise ValueError("Session limits must be positive")
        if self.compact_trigger_tokens is not None and self.compact_trigger_tokens < 1:
            raise ValueError("Session compact trigger must be positive or null")


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    created_at: str
    updated_at: str
    title: str | None = None
    last_prompt: str | None = None

    def __post_init__(self) -> None:
        created = _validate_session_timestamp(self.created_at, "created_at")
        updated = _validate_session_timestamp(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at cannot precede created_at")
        for name, value in (("title", self.title), ("last_prompt", self.last_prompt)):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be non-empty or null")


def _validate_session_timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


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
    """ContextPort 某一时刻读取的会话事实与模型工作集快照。

    ``messages`` 是 compact 后的模型工作集；``session_history`` 仅供需要从
    完整运行时事实派生状态的 attachment source 使用。``attachment_deliveries``
    保留本进程已经交付、但不写 Transcript 的上下文。
    """

    messages: tuple[ConversationMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = field(default_factory=tuple)
    session_history: tuple[ConversationMessage, ...] = field(default_factory=tuple)
    attachment_deliveries: tuple["AttachmentDelivery", ...] = field(
        default_factory=tuple
    )


@dataclass(frozen=True, slots=True)
class AttachmentDelivery:
    """已进入模型历史、但不写入 Transcript 的运行时 attachment。"""

    anchor_uuid: str
    attachment: ContextAttachment
    delivery_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not self.anchor_uuid or not self.delivery_id:
            raise ValueError("Attachment delivery ID and anchor must not be empty")
        if self.attachment.retention != "live_session":
            raise ValueError("Delivered attachments must use live_session retention")


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

    history: tuple[ConversationMessage, ...]
    working_set: tuple[ConversationMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = field(default_factory=tuple)
    compact_boundaries: tuple[CompactBoundary, ...] = field(default_factory=tuple)
    metadata: SessionMetadata | None = None
