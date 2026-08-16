"""上下文策略使用的 outbound port。"""

from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.context import ContextBudget, ContextPlan
from nano_code.agent.contracts.model import ModelMessage
from nano_code.agent.contracts.session import ContentReplacement, ConversationSnapshot
from nano_code.messages import ConversationMessage


@runtime_checkable
class ContextPort(Protocol):
    """将会话工作集转换为模型请求和上下文诊断。"""

    def plan(self, snapshot: ConversationSnapshot) -> ContextPlan: ...

    def inspect(self, snapshot: ConversationSnapshot) -> ContextBudget: ...

    def compaction_view(
        self, snapshot: ConversationSnapshot
    ) -> tuple[tuple[ModelMessage, ...], tuple[ContentReplacement, ...]]: ...

    def measure(self, messages: tuple[ConversationMessage, ...]) -> int: ...


__all__ = ["ContextPort"]
