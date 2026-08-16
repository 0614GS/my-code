"""Agent inbound port 返回的状态、历史投影和回合结果。"""

from dataclasses import dataclass

from nano_code.messages import TokenUsage
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.todos.models import TodoItem

from .context import ContextBudget


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """一次用户提示的终态数据。"""

    text: str
    turns: int
    usage: TokenUsage


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
    "AgentHistoryEntry",
    "AgentHistorySystemMessage",
    "AgentHistoryToolCall",
    "AgentHistoryUserMessage",
    "AgentSessionView",
    "AgentStatus",
    "AgentTurnResult",
]
