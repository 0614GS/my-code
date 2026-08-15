"""智能体核心、边界契约与查询编排。"""

from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.context import ContextBudget, ContextPlan
from nano_code.agent.contracts.inbound import (
    AgentContextState,
    AgentHistoryAssistantMessage,
    AgentHistoryEntry,
    AgentHistorySystemMessage,
    AgentHistoryToolCall,
    AgentHistoryUserMessage,
    AgentSessionView,
    AgentState,
    AgentTurnResult,
)
from nano_code.agent.contracts.model import (
    ModelContentBlock,
    ModelMessage,
    ModelResponseCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from nano_code.agent.contracts.session import (
    CompactBoundary,
    CompactTrigger,
    ContentReplacement,
    ConversationSnapshot,
    SessionSnapshot,
)
from nano_code.agent.contracts.tool import (
    ToolCallFinished,
    ToolCallStarted,
    ToolDefinition,
    ToolRoundCompleted,
    ToolRoundEvent,
)
from nano_code.agent.conversation import ConversationState
from nano_code.agent.engine import AgentEngine
from nano_code.agent.errors import ContextOverflow, ModelContextOverflow
from nano_code.agent.events import (
    AgentEvent,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
)
from nano_code.agent.ports import (
    AgentInboundPort,
    Compactor,
    ContextPort,
    ModelCompletionPort,
    ModelTurnPort,
    SessionRepository,
    ToolInteractionPort,
)

__all__ = [
    "AgentContextState",
    "AgentEngine",
    "AgentEvent",
    "AgentHistoryAssistantMessage",
    "AgentHistoryEntry",
    "AgentHistorySystemMessage",
    "AgentHistoryToolCall",
    "AgentHistoryUserMessage",
    "AgentInboundPort",
    "AgentSessionView",
    "AgentState",
    "AgentTextDelta",
    "AgentToolFinished",
    "AgentToolStarted",
    "AgentTurnCompleted",
    "AgentTurnResult",
    "CompactBoundary",
    "CompactTrigger",
    "CompactionOutcome",
    "Compactor",
    "ContentReplacement",
    "ContextBudget",
    "ContextPlan",
    "ContextPort",
    "ConversationSnapshot",
    "ConversationState",
    "ContextOverflow",
    "ModelCompletionPort",
    "ModelContextOverflow",
    "ModelContentBlock",
    "ModelMessage",
    "ModelResponseCompleted",
    "ModelStreamEvent",
    "ModelTextDelta",
    "ModelTurnPort",
    "SessionRepository",
    "SessionSnapshot",
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolDefinition",
    "ToolInteractionPort",
    "ToolRoundCompleted",
    "ToolRoundEvent",
]
