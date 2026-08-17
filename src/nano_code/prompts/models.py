"""provider 无关的结构化提示词值对象。"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class PromptStability(StrEnum):
    """提示词片段在请求之间保持不变的范围。"""

    STATIC = "static"
    SESSION = "session"
    REQUEST = "request"


type PromptResolver = Callable[[], str]


@dataclass(frozen=True, slots=True)
class PromptSection:
    """带稳定身份和延迟计算逻辑的提示词来源。"""

    key: str
    stability: PromptStability
    resolve: PromptResolver

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Prompt section key must not be empty")


@dataclass(frozen=True, slots=True)
class ResolvedPromptSection:
    """一次请求中已经计算完成的提示词片段。"""

    key: str
    content: str
    stability: PromptStability

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Resolved prompt section key must not be empty")
        if not self.content.strip():
            raise ValueError(f"Prompt section {self.key!r} resolved to empty content")


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    """保留片段边界的完整 system prompt。"""

    sections: tuple[ResolvedPromptSection, ...]

    def __post_init__(self) -> None:
        if not self.sections:
            raise ValueError("System prompt must contain at least one section")

    @property
    def text(self) -> str:
        """返回不需要结构化 prompt block 的 provider 可用文本。"""

        return "\n\n".join(section.content for section in self.sections)

    @classmethod
    def from_text(
        cls,
        content: str,
        *,
        key: str = "request",
        stability: PromptStability = PromptStability.REQUEST,
    ) -> "SystemPrompt":
        """为摘要等独立请求构造单片段提示词。"""

        return cls((ResolvedPromptSection(key, content, stability),))
