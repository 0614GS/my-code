"""Request-time context contracts.

Concrete planners, resolvers, and compaction services live in explicit
submodules so importing a context value object does not initialize adapters.
"""

from nano_code.context.attachments.models import (
    AttachmentContent,
    AttachmentRetention,
    ContextAttachment,
    ContextObservation,
)
from nano_code.context.compaction import (
    CompactionCoordinator,
    CompactionResult,
    CompactionService,
)
from nano_code.context.documents import (
    ContextDocumentContent,
    ContextInstruction,
    ContextInstructionKind,
    UserContextDocument,
)
from nano_code.context.models import (
    CompactionOutcome,
    ContextBudget,
    ContextOverflow,
    ContextPlan,
)
from nano_code.context.planner import ContextBuilder
from nano_code.context.session import (
    AttachmentDelivery,
    ContextSession,
    ContextSnapshot,
)

__all__ = [
    "AttachmentContent",
    "AttachmentDelivery",
    "AttachmentRetention",
    "ContextAttachment",
    "ContextObservation",
    "CompactionOutcome",
    "CompactionResult",
    "CompactionCoordinator",
    "CompactionService",
    "ContextBudget",
    "ContextBuilder",
    "ContextOverflow",
    "ContextPlan",
    "ContextSession",
    "ContextSnapshot",
    "ContextDocumentContent",
    "ContextInstruction",
    "ContextInstructionKind",
    "UserContextDocument",
]
