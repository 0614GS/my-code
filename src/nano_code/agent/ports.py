"""Agent 核心拥有的 inbound/outbound ports。

Port 的声明者是使用者：上层通过 ``AgentInboundPort`` 驱动核心，核心通过
下方 outbound ports 请求会话、上下文、模型、工具和 compact 能力。具体包只
实现这些协议，不在各自的 adapter 包中重新声明 Agent-facing Protocol。
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.context import (
    ContextBudget,
    ContextPlan,
)
from nano_code.agent.contracts.inbound import (
    AgentContextState,
    AgentSessionView,
    AgentState,
    AgentTurnResult,
)
from nano_code.agent.contracts.model import ModelMessage, ModelStreamEvent
from nano_code.agent.contracts.session import (
    CompactBoundary,
    CompactTrigger,
    ContentReplacement,
    ConversationSnapshot,
    SessionSnapshot,
)
from nano_code.agent.contracts.tool import ToolRoundEvent
from nano_code.agent.events import AgentEvent
from nano_code.messages import ChatMessage, ModelResponse, ToolResultBlock, ToolUseBlock
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation


@runtime_checkable
class AgentInboundPort(Protocol):
    """CLI、TUI 或其它 driving adapter 可使用的 Agent 能力。"""

    @property
    def session_id(self) -> str:
        """当前绑定 session 的稳定标识。"""
        ...

    @property
    def working_messages(self) -> tuple[ChatMessage, ...]:
        """当前 compact 工作集的只读快照。"""
        ...

    @property
    def message_count(self) -> int: ...

    @property
    def content_replacement_count(self) -> int: ...

    @property
    def compact_count(self) -> int: ...

    async def submit(self, prompt: str) -> AgentTurnResult:
        """运行一个用户回合并等待终态。"""
        ...

    def stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """运行一个用户回合并产生可观察事件。"""
        ...

    def state(self) -> AgentState:
        """返回当前 session 的只读状态。"""
        ...

    def context_state(self) -> AgentContextState:
        """返回当前上下文预算和 compact 诊断。"""
        ...

    async def compact(self, trigger: CompactTrigger = "manual") -> CompactBoundary:
        """生成并提交一次 compact boundary。"""
        ...

    def resume(self, repository: "SessionRepository") -> AgentSessionView:
        """校验并切换到另一个 session，同时返回历史投影。"""
        ...


@runtime_checkable
class ContextPort(Protocol):
    """将会话工作集转换为模型请求和上下文诊断。"""

    def plan(self, snapshot: ConversationSnapshot) -> ContextPlan: ...

    def inspect(self, snapshot: ConversationSnapshot) -> ContextBudget: ...

    def compaction_view(
        self, snapshot: ConversationSnapshot
    ) -> tuple[tuple[ModelMessage, ...], tuple[ContentReplacement, ...]]: ...

    def measure(self, messages: tuple[ChatMessage, ...]) -> int: ...


@runtime_checkable
class ModelTurnPort(Protocol):
    """主 Agent Loop 使用的流式模型回合能力。"""

    def stream(self, request: ContextPlan) -> AsyncIterator[ModelStreamEvent]: ...


@runtime_checkable
class ModelCompletionPort(Protocol):
    """compact 等独立请求使用的完整模型响应能力。"""

    async def complete(self, request: ContextPlan) -> ModelResponse: ...


@runtime_checkable
class ToolInteractionPort(Protocol):
    """Agent 使用的一次工具轮次及 session-scoped 展示能力。"""

    def run_round(
        self,
        calls: tuple[ToolUseBlock, ...],
        assistant_message: ChatMessage,
    ) -> AsyncIterator[ToolRoundEvent]: ...

    def present_use(self, call: ToolUseBlock) -> ToolUsePresentation: ...

    def present_stored_result(
        self,
        call: ToolUseBlock,
        result: ToolResultBlock | None,
    ) -> ToolResultPresentation: ...

    def bind_session(self, session_id: str) -> None:
        """切换当前工具结果和展示快照的 session 归属。"""
        ...


@runtime_checkable
class SessionRepository(Protocol):
    """ConversationState 需要的追加式会话事实来源。"""

    @property
    def session_id(self) -> str: ...

    def snapshot(self) -> SessionSnapshot: ...

    def append(self, message: ChatMessage) -> None: ...

    def append_content_replacement(self, replacement: ContentReplacement) -> None: ...

    def append_compact_boundary(self, boundary: CompactBoundary) -> None: ...


@runtime_checkable
class Compactor(Protocol):
    """生成尚未持久化的 compact 提交计划。"""

    async def compact(
        self,
        snapshot: ConversationSnapshot,
        trigger: CompactTrigger,
    ) -> CompactionOutcome: ...
