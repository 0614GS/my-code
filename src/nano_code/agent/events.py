"""供交互式前端消费的智能体循环可观察事件。"""

from dataclasses import dataclass

from nano_code.agent.contracts.inbound import AgentMaxStepsReached, AgentTurnSucceeded
from nano_code.messages import JsonObject
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.todos.models import TodoItem


@dataclass(frozen=True, slots=True)
class AgentTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AgentToolStarted:
    tool_use_id: str
    name: str
    input: JsonObject
    presentation: ToolUsePresentation


@dataclass(frozen=True, slots=True)
class AgentToolFinished:
    tool_use_id: str
    name: str
    is_error: bool
    presentation: ToolResultPresentation


@dataclass(frozen=True, slots=True)
class AgentTodoListUpdated:
    """已提交的会话事实产生了新的 TodoList 投影。"""

    todos: tuple[TodoItem, ...]


@dataclass(frozen=True, slots=True)
class AgentTurnCompleted:
    result: AgentTurnSucceeded


@dataclass(frozen=True, slots=True)
class AgentStepLimitReached:
    result: AgentMaxStepsReached


type AgentEvent = (
    AgentTextDelta
    | AgentToolStarted
    | AgentToolFinished
    | AgentTodoListUpdated
    | AgentTurnCompleted
    | AgentStepLimitReached
)
