"""Agent inbound port 返回的状态、历史投影和回合结果。"""

from dataclasses import dataclass

from nano_code.application.chat.presentation import (
    ToolResultPresentation,
    ToolUsePresentation,
)
from nano_code.context.attachments.models import ContextAttachment
from nano_code.conversation import ReasoningPresentation, TokenUsage
from nano_code.features.todos.models import TodoItem

from .context import ContextBudget


@dataclass(frozen=True, slots=True)
class AgentTurnInput:
    """一次用户回合及其在提交前已准备好的事件 attachment。"""

    prompt: str
    attachments: tuple[ContextAttachment, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("Prompt must not be empty")
        if any(
            attachment.retention != "live_session" for attachment in self.attachments
        ):
            raise ValueError("Agent turn attachments must use live_session retention")


@dataclass(frozen=True, slots=True)
class AgentTurnSucceeded:
    """一次用户提示正常完成后的终态数据。"""

    text: str
    completed_steps: int
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class AgentMaxStepsReached:
    """显式 Step 上限终止了当前用户回合。"""

    max_steps: int
    completed_steps: int
    usage: TokenUsage


type AgentTurnOutcome = AgentTurnSucceeded | AgentMaxStepsReached


@dataclass(frozen=True, slots=True)
class AgentStatus:
    """Agent inbound port 暴露的只读会话状态。"""

    session_id: str
    working_message_count: int
    history_message_count: int
    content_replacement_count: int
    compact_count: int
    todos: tuple[TodoItem, ...]


@dataclass(frozen=True, slots=True)
class AgentContextStatus:
    """Agent inbound port 暴露的上下文诊断。"""

    budget: ContextBudget
    working_message_count: int
    replacement_count: int
    compact_count: int


@dataclass(frozen=True, slots=True)
class AgentHistoryUserMessage:
    text: str


@dataclass(frozen=True, slots=True)
class AgentHistoryAssistantMessage:
    text: str


@dataclass(frozen=True, slots=True)
class AgentHistoryReasoning:
    id: str
    presentation: ReasoningPresentation


@dataclass(frozen=True, slots=True)
class AgentHistorySystemMessage:
    text: str


@dataclass(frozen=True, slots=True)
class AgentHistoryToolCall:
    tool_use_id: str
    use: ToolUsePresentation
    result: ToolResultPresentation
    is_error: bool


type AgentHistoryEntry = (
    AgentHistoryUserMessage
    | AgentHistoryAssistantMessage
    | AgentHistoryReasoning
    | AgentHistorySystemMessage
    | AgentHistoryToolCall
)


@dataclass(frozen=True, slots=True)
class AgentSessionView:
    """恢复会话后同时返回状态和 UI 无关的历史投影。"""

    status: AgentStatus
    history: tuple[AgentHistoryEntry, ...]


__all__ = [
    "AgentContextStatus",
    "AgentHistoryAssistantMessage",
    "AgentHistoryReasoning",
    "AgentHistoryEntry",
    "AgentHistorySystemMessage",
    "AgentHistoryToolCall",
    "AgentHistoryUserMessage",
    "AgentSessionView",
    "AgentStatus",
    "AgentTurnInput",
    "AgentMaxStepsReached",
    "AgentTurnOutcome",
    "AgentTurnSucceeded",
]
