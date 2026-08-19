"""供交互式前端消费的智能体循环可观察事件。"""

from dataclasses import dataclass

from nano_code.agent.models import AgentMaxStepsReached, AgentTurnSucceeded
from nano_code.model import (
    JsonObject,
    ReasoningDisclosure,
    ReasoningPresentation,
)
from nano_code.tools import (
    ToolResultPresentation,
    ToolUsePresentation,
)


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


@dataclass(frozen=True, slots=True)
class AgentTurnCompleted:
    result: AgentTurnSucceeded


@dataclass(frozen=True, slots=True)
class AgentStepLimitReached:
    result: AgentMaxStepsReached


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
    | AgentTurnCompleted
    | AgentStepLimitReached
)
