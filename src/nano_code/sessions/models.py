"""会话日志中除普通消息外的结构化记录。"""

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

CompactTrigger = Literal["auto", "manual", "reactive"]


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
