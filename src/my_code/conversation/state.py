"""Pure in-memory conversation aggregate and compaction facts."""

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

type CompactTrigger = Literal["auto", "manual", "reactive"]


@dataclass(frozen=True, slots=True)
class ContentReplacement:
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
class CompactBoundary:
    parent_uuid: str
    summary_uuid: str
    trigger: CompactTrigger
    pre_compact_chars: int
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if self.pre_compact_chars < 1:
            raise ValueError("pre_compact_chars must be positive")


__all__ = [
    "CompactBoundary",
    "CompactTrigger",
    "ContentReplacement",
]
