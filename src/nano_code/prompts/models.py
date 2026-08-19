"""provider 无关的结构化提示词值对象。"""

from collections.abc import Callable
from dataclasses import dataclass

from nano_code.model import PromptStability

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
