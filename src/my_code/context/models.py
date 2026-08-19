"""Context build results and request-local budget diagnostics."""

from dataclasses import dataclass, field
from typing import Literal

from my_code.context.session import AttachmentDelivery
from my_code.conversation.models import ConversationSummaryMessage
from my_code.conversation.state import CompactBoundary, ContentReplacement
from my_code.model.capabilities import CapabilitySource, ModelLimits
from my_code.model.primitives import ProviderBinding, TokenUsage
from my_code.model.request import ModelRequest


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Observable budget for one projected model request."""

    message_limit_chars: int
    message_chars: int
    system_chars: int
    tool_schema_chars: int
    reserved_output_tokens: int
    last_actual_input_tokens: int | None
    incremental_tokens: int
    estimated_input_tokens: int
    user_context_chars: int = 0
    attachment_chars: int = 0
    input_tokens: int = 0
    input_limit_tokens: int = 200_000
    compact_trigger_tokens: int = 180_000
    last_reported_input_tokens: int | None = None
    measurement: Literal["reported_calibrated", "tokenizer_estimate"] = (
        "tokenizer_estimate"
    )
    model_limits: ModelLimits = ModelLimits()
    model_limit_source: CapabilitySource = CapabilitySource.FALLBACK
    configured_compact_trigger_tokens: int | None = None
    warning: str | None = None

    @property
    def estimated_input_chars(self) -> int:
        return (
            self.message_chars
            + self.system_chars
            + self.tool_schema_chars
            + self.user_context_chars
            + self.attachment_chars
        )

    @property
    def estimated_total_tokens(self) -> int:
        return self.input_tokens + self.reserved_output_tokens

    @property
    def remaining_input_tokens(self) -> int:
        return max(0, self.input_limit_tokens - self.input_tokens)


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """A model request plus decisions that must be committed before use."""

    request: ModelRequest
    budget: ContextBudget | None = None
    new_content_replacements: tuple[ContentReplacement, ...] = field(
        default_factory=tuple
    )
    new_attachment_deliveries: tuple[AttachmentDelivery, ...] = field(
        default_factory=tuple
    )
    request_binding: ProviderBinding | None = None
    request_input_tokens_estimate: int | None = None


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """A compaction proposal with no persistence side effect."""

    replacements: tuple[ContentReplacement, ...]
    summary: ConversationSummaryMessage
    boundary: CompactBoundary
    usage: TokenUsage


class ContextOverflow(RuntimeError):
    """The context cannot construct a request within the active model limit."""

    def __init__(self, current_size: int, maximum_size: int) -> None:
        self.current_size = current_size
        self.maximum_size = maximum_size
        super().__init__(
            f"Context requires {current_size} units but the limit is {maximum_size}"
        )


__all__ = [
    "CompactionOutcome",
    "ContextBudget",
    "ContextOverflow",
    "ContextPlan",
]
