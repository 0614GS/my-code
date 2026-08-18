"""智能体核心、边界契约与查询编排。"""

from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.context import (
    ContextBudget,
    ContextPlan,
)
from nano_code.agent.contracts.inbound import (
    AgentContextStatus,
    AgentHistoryAssistantMessage,
    AgentHistoryEntry,
    AgentHistorySystemMessage,
    AgentHistoryToolCall,
    AgentHistoryUserMessage,
    AgentMaxStepsReached,
    AgentSessionView,
    AgentStatus,
    AgentTurnInput,
    AgentTurnOutcome,
    AgentTurnSucceeded,
)
from nano_code.agent.contracts.model import (
    ModelAssistantMessage,
    ModelMessage,
    ModelOpaqueAssistantBlock,
    ModelOutput,
    ModelOutputCompleted,
    ModelRequest,
    ModelStreamEvent,
    ModelTextBlock,
    ModelTextDelta,
    ModelToolDefinition,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
)
from nano_code.agent.contracts.session import (
    AttachmentDelivery,
    CompactBoundary,
    CompactTrigger,
    ContentReplacement,
    ConversationSnapshot,
    SessionMetadata,
    SessionSnapshot,
    SessionStart,
)
from nano_code.agent.contracts.tool import (
    ToolCallFinished,
    ToolCallStarted,
    ToolRoundCompleted,
    ToolRoundEvent,
)
from nano_code.agent.conversation import ConversationState
from nano_code.agent.engine import AgentEngine
from nano_code.agent.errors import ContextOverflow, ModelContextOverflow
from nano_code.agent.events import (
    AgentEvent,
    AgentStepLimitReached,
    AgentTextDelta,
    AgentTodoListUpdated,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
)
from nano_code.agent.ports.compaction import CompactorPort
from nano_code.agent.ports.context import ContextPort
from nano_code.agent.ports.inbound import AgentInboundPort
from nano_code.agent.ports.model import ModelCallPort, ModelCompletionPort
from nano_code.agent.ports.session import SessionRepository
from nano_code.agent.ports.tool import ToolRoundPort

__all__ = [
    "AgentContextStatus",
    "AgentEngine",
    "AgentEvent",
    "AgentHistoryAssistantMessage",
    "AgentHistoryEntry",
    "AgentHistorySystemMessage",
    "AgentHistoryToolCall",
    "AgentHistoryUserMessage",
    "AgentInboundPort",
    "AgentMaxStepsReached",
    "AgentSessionView",
    "AgentStatus",
    "AgentTextDelta",
    "AgentTodoListUpdated",
    "AgentToolFinished",
    "AgentToolStarted",
    "AgentTurnCompleted",
    "AgentStepLimitReached",
    "AgentTurnOutcome",
    "AgentTurnInput",
    "AgentTurnSucceeded",
    "CompactBoundary",
    "AttachmentDelivery",
    "CompactTrigger",
    "CompactionOutcome",
    "CompactorPort",
    "ContentReplacement",
    "ContextBudget",
    "ContextPlan",
    "ContextPort",
    "ConversationSnapshot",
    "ConversationState",
    "ContextOverflow",
    "ModelCallPort",
    "ModelCompletionPort",
    "ModelContextOverflow",
    "ModelAssistantMessage",
    "ModelMessage",
    "ModelOutput",
    "ModelOutputCompleted",
    "ModelOpaqueAssistantBlock",
    "ModelRequest",
    "ModelStreamEvent",
    "ModelTextBlock",
    "ModelTextDelta",
    "ModelToolDefinition",
    "ModelToolResultBlock",
    "ModelToolUseBlock",
    "ModelUserMessage",
    "SessionRepository",
    "SessionMetadata",
    "SessionSnapshot",
    "SessionStart",
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolRoundPort",
    "ToolRoundCompleted",
    "ToolRoundEvent",
]
