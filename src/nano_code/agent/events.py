"""供交互式前端消费的智能体循环可观察事件。"""

from dataclasses import dataclass

from nano_code.agent.contracts.inbound import AgentTurnResult
from nano_code.messages import JsonObject
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation


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
class AgentTurnCompleted:
    result: AgentTurnResult


type AgentEvent = (
    AgentTextDelta | AgentToolStarted | AgentToolFinished | AgentTurnCompleted
)
