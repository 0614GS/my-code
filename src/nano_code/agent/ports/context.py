"""上下文策略使用的 outbound port。"""

from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.context import ContextBudget, ContextPlan
from nano_code.context import ContextSnapshot
from nano_code.conversation import ContentReplacement, ConversationMessage
from nano_code.model import ModelMessage


@runtime_checkable
class ContextPort(Protocol):
    """将会话工作集转换为模型请求和上下文诊断。"""

    def plan(self, snapshot: ContextSnapshot) -> ContextPlan: ...

    def inspect(self, snapshot: ContextSnapshot) -> ContextBudget: ...

    def compaction_view(
        self, snapshot: ContextSnapshot
    ) -> tuple[tuple[ModelMessage, ...], tuple[ContentReplacement, ...]]: ...

    def measure(self, messages: tuple[ConversationMessage, ...]) -> int: ...


__all__ = ["ContextPort"]
