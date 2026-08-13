"""供交互式前端消费的智能体循环可观察事件。"""

from dataclasses import dataclass

from nano_code.agent.engine_types import AgentTurnResult
from nano_code.messages import JsonObject


@dataclass(frozen=True, slots=True)
class AgentTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AgentToolStarted:
    tool_use_id: str
    name: str
    input: JsonObject


@dataclass(frozen=True, slots=True)
class AgentToolFinished:
    tool_use_id: str
    name: str
    content: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class AgentTurnCompleted:
    result: AgentTurnResult


type AgentEvent = (
    AgentTextDelta | AgentToolStarted | AgentToolFinished | AgentTurnCompleted
)
