"""Context build results and request-local budget diagnostics."""

from dataclasses import dataclass, field
from typing import Literal

from my_code.conversation.models import ConversationSummaryMessage
from my_code.conversation.state import CompactBoundary, ContentReplacement
from my_code.model.capabilities import CapabilitySource, ModelLimits
from my_code.model.invocation import ModelInputOrigin
from my_code.model.primitives import ContextFootprint, ProviderBinding, TokenUsage
from my_code.model.request import ModelRequest


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Observable budget for one projected model request."""

    reported_base_tokens: int | None
    estimated_delta_tokens: int
    projected_tokens: int
    reserved_output_tokens: int
    input_limit_tokens: int = 200_000
    compact_trigger_tokens: int = 180_000
    measurement: Literal["reported", "estimated"] = "estimated"
    model_limits: ModelLimits = ModelLimits()
    model_limit_source: CapabilitySource = CapabilitySource.FALLBACK
    configured_compact_trigger_tokens: int | None = None
    warning: str | None = None
    cache_hit_rate: float | None = None

    @property
    def estimated_total_tokens(self) -> int:
        return self.projected_tokens + self.reserved_output_tokens

    @property
    def remaining_input_tokens(self) -> int:
        return max(0, self.input_limit_tokens - self.projected_tokens)


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """A model request plus decisions that must be committed before use."""

    request: ModelRequest
    provenance: tuple[ModelInputOrigin, ...] = field(default_factory=tuple)
    budget: ContextBudget | None = None
    new_content_replacements: tuple[ContentReplacement, ...] = field(
        default_factory=tuple
    )
    request_binding: ProviderBinding | None = None
    request_footprint: ContextFootprint | None = None

    def __post_init__(self) -> None:
        if self.provenance and len(self.provenance) != len(self.request.input):
            raise ValueError("Context provenance must match request input")


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """A side-effect-free proposal whose usage aggregates completed attempts."""

    replacements: tuple[ContentReplacement, ...]
    summary: ConversationSummaryMessage
    boundary: CompactBoundary
    usage: TokenUsage


class ContextOverflow(RuntimeError):
    """The context cannot construct a request within the active model limit."""

    def __init__(
        self,
        current_size: int,
        maximum_size: int,
        replacements: tuple[ContentReplacement, ...] = (),
        budget: ContextBudget | None = None,
    ) -> None:
        self.current_size = current_size
        self.maximum_size = maximum_size
        self.replacements = replacements
        self.budget = budget
        super().__init__(
            f"Context requires {current_size} units but the limit is {maximum_size}"
        )


__all__ = [
    "CompactionOutcome",
    "ContextBudget",
    "ContextOverflow",
    "ContextPlan",
]
