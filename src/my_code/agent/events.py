"""供交互式前端消费的智能体循环可观察事件。"""

from dataclasses import dataclass

from my_code.agent.models import AgentMaxStepsReached, AgentTurnSucceeded
from my_code.conversation.presentation import ToolResultPresentation
from my_code.foundation.json import JsonObject
from my_code.model.primitives import ReasoningDisclosure, ReasoningPresentation
from my_code.tools.presentation import ToolUsePresentation


@dataclass(frozen=True, slots=True)
class AgentTextStarted:
    pass


@dataclass(frozen=True, slots=True)
class AgentTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AgentTextCompleted:
    text: str


@dataclass(frozen=True, slots=True)
class AgentReasoningStarted:
    disclosure: ReasoningDisclosure


@dataclass(frozen=True, slots=True)
class AgentReasoningDelta:
    disclosure: ReasoningDisclosure
    part_index: int
    text: str


@dataclass(frozen=True, slots=True)
class AgentReasoningCompleted:
    presentation: ReasoningPresentation


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
class AgentConversationUpdated:
    """New canonical conversation facts have been committed."""

    pass


type AgentEvent = (
    AgentTextStarted
    | AgentTextDelta
    | AgentTextCompleted
    | AgentReasoningStarted
    | AgentReasoningDelta
    | AgentReasoningCompleted
    | AgentToolStarted
    | AgentToolFinished
    | AgentConversationUpdated
    | AgentTurnSucceeded
    | AgentMaxStepsReached
)


__all__ = [
    "AgentConversationUpdated",
    "AgentEvent",
    "AgentReasoningCompleted",
    "AgentReasoningDelta",
    "AgentReasoningStarted",
    "AgentTextCompleted",
    "AgentTextDelta",
    "AgentTextStarted",
    "AgentToolFinished",
    "AgentToolStarted",
]
