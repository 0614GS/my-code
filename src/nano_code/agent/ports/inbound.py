"""驱动 Agent 核心的 inbound port。"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.inbound import (
    AgentContextStatus,
    AgentSessionView,
    AgentStatus,
    AgentTurnOutcome,
)
from nano_code.agent.contracts.session import CompactBoundary, CompactTrigger
from nano_code.agent.events import AgentEvent

from .session import SessionRepository


@runtime_checkable
class AgentInboundPort(Protocol):
    """CLI、TUI 或其它 driving adapter 可使用的 Agent 能力。"""

    async def submit(self, prompt: str) -> AgentTurnOutcome:
        """运行一个用户回合并等待终态。"""
        ...

    def stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """运行一个用户回合并产生可观察事件。"""
        ...

    def status(self) -> AgentStatus:
        """返回当前 session 的只读状态。"""
        ...

    def context_status(self) -> AgentContextStatus:
        """返回当前上下文预算和 compact 诊断。"""
        ...

    async def compact(self, trigger: CompactTrigger = "manual") -> CompactBoundary:
        """生成并提交一次 compact boundary。"""
        ...

    def resume(self, repository: SessionRepository) -> AgentSessionView:
        """校验并切换到另一个 session，同时返回历史投影。"""
        ...


__all__ = ["AgentInboundPort"]
