"""供交互式前端消费的智能体循环可观察事件。"""

from dataclasses import dataclass

from my_code.agent.models import AgentMaxStepsReached, AgentTurnSucceeded
from my_code.conversation.presentation import ToolResultPresentation
from my_code.conversation.state import CompactTrigger
from my_code.foundation.json import JsonObject
from my_code.model.primitives import (
    ReasoningDisclosure,
    ReasoningPresentation,
    TokenUsage,
)
from my_code.tools.presentation import ToolUsePresentation


@dataclass(frozen=True, slots=True)
class PreparedContextItem:
    audit_id: str
    source: str
    attachment_kind: str | None
    text: str


@dataclass(frozen=True, slots=True)
class AgentModelRequestPrepared:
    """A semantic model request is durable and may now be delivered."""

    request_id: str
    request_number: int
    purpose: str
    injections: tuple[PreparedContextItem, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentInputAccepted:
    """A host input became a canonical HumanMessage at a safe boundary."""

    input_id: str | None
    prompt: str


@dataclass(frozen=True, slots=True)
class AgentInputFailed:
    """A host input could not be prepared and remains non-canonical."""

    input_id: str
    prompt: str
    error: str


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
class AgentPlanStarted:
    pass


@dataclass(frozen=True, slots=True)
class AgentPlanDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AgentPlanCompleted:
    plan: str


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
class AgentModelStepCompleted:
    """A complete AssistantMessage is durable and can now be classified."""

    step_index: int
    has_tools: bool


@dataclass(frozen=True, slots=True)
class AgentCompactionStarted:
    """A full context compaction started before the next model request."""

    trigger: CompactTrigger


@dataclass(frozen=True, slots=True)
class AgentCompactionCompleted:
    """A full context compaction was committed durably."""

    trigger: CompactTrigger
    usage: TokenUsage


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
    AgentModelRequestPrepared
    | AgentCompactionStarted
    | AgentCompactionCompleted
    | AgentInputAccepted
    | AgentInputFailed
    | AgentTextStarted
    | AgentTextDelta
    | AgentTextCompleted
    | AgentPlanStarted
    | AgentPlanDelta
    | AgentPlanCompleted
    | AgentReasoningStarted
    | AgentReasoningDelta
    | AgentReasoningCompleted
    | AgentModelStepCompleted
    | AgentToolStarted
    | AgentToolFinished
    | AgentConversationUpdated
    | AgentTurnSucceeded
    | AgentMaxStepsReached
)


__all__ = [
    "AgentCompactionCompleted",
    "AgentCompactionStarted",
    "AgentConversationUpdated",
    "AgentInputAccepted",
    "AgentInputFailed",
    "AgentModelStepCompleted",
    "AgentModelRequestPrepared",
    "AgentPlanCompleted",
    "AgentPlanDelta",
    "AgentPlanStarted",
    "AgentEvent",
    "AgentReasoningCompleted",
    "AgentReasoningDelta",
    "AgentReasoningStarted",
    "AgentTextCompleted",
    "AgentTextDelta",
    "AgentTextStarted",
    "AgentToolFinished",
    "AgentToolStarted",
    "PreparedContextItem",
]
