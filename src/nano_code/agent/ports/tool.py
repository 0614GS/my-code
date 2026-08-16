"""工具轮次使用的 outbound port。"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.tool import ToolRoundEvent
from nano_code.messages import AssistantMessage, ToolCall, ToolResult
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation


@runtime_checkable
class ToolRoundPort(Protocol):
    """Agent 使用的一次工具轮次及 session-scoped 展示能力。"""

    def run_round(
        self,
        calls: tuple[ToolCall, ...],
        assistant_message: AssistantMessage,
    ) -> AsyncIterator[ToolRoundEvent]: ...

    def present_use(self, call: ToolCall) -> ToolUsePresentation: ...

    def present_stored_result(
        self,
        call: ToolCall,
        result: ToolResult | None,
    ) -> ToolResultPresentation: ...

    def bind_session(self, session_id: str) -> None:
        """切换当前工具结果和展示快照的 session 归属。"""
        ...


__all__ = ["ToolRoundPort"]
