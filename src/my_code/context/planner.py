"""从会话快照生成完整 ModelRequest。"""

from collections.abc import Callable
from typing import Literal

from my_code.context.attachments.sources import DerivedAttachmentResolver
from my_code.context.documents import UserContextDocument
from my_code.context.meter import ContextMeter
from my_code.context.microcompact import (
    MicrocompactPolicy,
    apply_content_replacements,
)
from my_code.context.models import ContextBudget, ContextOverflow, ContextPlan
from my_code.context.normalization import ModelInputNormalizer
from my_code.context.session_cache import (
    AttachmentProjectionInput,
    ContextPlanningInput,
    SessionContextCache,
)
from my_code.context.user_context import EmptyUserContextResolver, UserContextResolver
from my_code.conversation.attachments import AttachmentPayload
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
    ToolResultBatch,
)
from my_code.conversation.state import ContentReplacement
from my_code.model.capabilities import (
    FALLBACK_INPUT_TOKENS,
    ActiveModelEnvironment,
    fallback_descriptor,
    resolve_environment,
)
from my_code.model.invocation import ModelInputOrigin, ModelInputOriginKind
from my_code.model.primitives import (
    ContextFootprint,
    ProviderBinding,
    ProviderReplayRecord,
    TokenUsage,
)
from my_code.model.request import (
    AssistantOutput,
    ModelInputItem,
    ModelRequest,
    ModelToolDefinition,
    SystemPrompt,
)
from my_code.prompts.registry import PromptRegistry


class ContextPlanner:
    """集中拥有 ConversationEntry → ModelInputItem 投影边界。"""

    def __init__(
        self,
        *,
        prompt: PromptRegistry,
        max_output_tokens: int,
        normalizer: ModelInputNormalizer | None = None,
        microcompact: MicrocompactPolicy | None = None,
        user_context_resolver: UserContextResolver | None = None,
        attachment_resolver: DerivedAttachmentResolver | None = None,
        binding_resolver: Callable[[], ProviderBinding] | None = None,
        model_environment: Callable[[], ActiveModelEnvironment] | None = None,
        meter: ContextMeter | None = None,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.prompt = prompt
        self.max_output_tokens = max_output_tokens
        self.normalizer = normalizer or ModelInputNormalizer()
        self.microcompact = microcompact or MicrocompactPolicy()
        self.user_context_resolver = user_context_resolver or EmptyUserContextResolver()
        self.attachment_resolver = attachment_resolver or DerivedAttachmentResolver()
        self.binding_resolver = binding_resolver
        fallback_environment = resolve_environment(
            fallback_descriptor("unknown"),
            requested_output_tokens=max_output_tokens,
            configured_trigger_tokens=None,
        )
        self._model_environment = model_environment or (lambda: fallback_environment)
        self.meter = meter or ContextMeter()
        self.attachment_projector = self.normalizer.attachment_projector

    def plan(
        self,
        state: ContextPlanningInput,
        runtime: SessionContextCache,
        *,
        tools: tuple[ModelToolDefinition, ...],
    ) -> ContextPlan:
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        effective = apply_content_replacements(
            state.context_entries, state.content_replacements
        )
        proposed: tuple[ContentReplacement, ...] = ()
        user_context = runtime.user_context(self.user_context_resolver.resolve)
        model_messages = self.normalizer.normalize(
            user_context,
            effective,
            state.replay_records,
            active_binding=binding,
        )
        system_prompt = runtime.resolve_prompt(self.prompt)
        request = ModelRequest(
            system_prompt, model_messages, tools, self.max_output_tokens
        )
        budget, footprint = self._budget(effective, request)
        if budget.projected_tokens >= budget.compact_trigger_tokens:
            proposed = self.microcompact.propose(
                state.context_entries,
                state.content_replacements,
                current_tokens=budget.projected_tokens,
                trigger_tokens=budget.compact_trigger_tokens,
                estimate=lambda view: self._projected_tokens_for(
                    view,
                    user_context,
                    state.replay_records,
                    system_prompt,
                    tools,
                ),
            )
            if proposed:
                effective = apply_content_replacements(
                    state.context_entries,
                    state.content_replacements + proposed,
                )
                model_messages = self.normalizer.normalize(
                    user_context,
                    effective,
                    state.replay_records,
                    active_binding=binding,
                )
                request = ModelRequest(
                    system_prompt, model_messages, tools, self.max_output_tokens
                )
                budget, footprint = self._budget(effective, request)
        if budget.projected_tokens >= budget.compact_trigger_tokens:
            raise ContextOverflow(
                budget.projected_tokens,
                budget.input_limit_tokens,
                proposed,
                budget,
            )
        return ContextPlan(
            request=request,
            provenance=_request_provenance(
                user_context,
                effective,
                state.content_replacements + proposed,
            ),
            budget=budget,
            new_content_replacements=proposed,
            request_binding=binding,
            request_footprint=footprint,
        )

    def inspect(
        self,
        state: ContextPlanningInput,
        runtime: SessionContextCache,
        *,
        tools: tuple[ModelToolDefinition, ...],
    ) -> ContextBudget:
        effective = apply_content_replacements(
            state.context_entries, state.content_replacements
        )
        user_context = runtime.user_context(self.user_context_resolver.resolve)
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        messages = self.normalizer.normalize(
            user_context,
            effective,
            state.replay_records,
            active_binding=binding,
        )
        system_prompt = runtime.resolve_prompt(self.prompt)
        request = ModelRequest(
            system_prompt,
            messages,
            tools,
            self.max_output_tokens,
        )
        budget, _ = self._budget(effective, request)
        return budget

    def compaction_view(
        self, state: ContextPlanningInput
    ) -> tuple[tuple[ModelInputItem, ...], tuple[ContentReplacement, ...]]:
        effective = apply_content_replacements(
            state.context_entries, state.content_replacements
        )
        return (
            self.normalizer.normalize_transcript(effective),
            (),
        )

    def measure(
        self, messages: tuple[ConversationEntry, ...]
    ) -> tuple[int, Literal["reported", "estimated"]]:
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        anchor = _usage_anchor(messages, binding)
        if anchor is not None:
            return (
                anchor.usage.total_input_tokens + anchor.usage.output_tokens,
                "reported",
            )
        model = self.normalizer.normalize_transcript(messages)
        request = ModelRequest(SystemPrompt.from_text("context"), model, (), 1)
        tokens = self.meter.estimate(binding, self.meter.footprint(request)).tokens
        return tokens, "estimated"

    def record_response(
        self,
        plan: ContextPlan,
        response: AssistantOutput,
        usage: TokenUsage,
    ) -> ContextFootprint:
        if plan.request_footprint is None:
            raise ValueError("Context plan is missing its request footprint")
        self.meter.calibrate(plan.request_binding, plan.request_footprint, usage)
        return self.meter.response_footprint(plan.request, response)

    def derive_attachments(
        self, state: AttachmentProjectionInput
    ) -> tuple[AttachmentPayload, ...]:
        return self.attachment_resolver.resolve(state)

    def acknowledge_attachments(
        self,
        attachments: tuple[AttachmentPayload, ...],
    ) -> None:
        self.attachment_resolver.acknowledge(attachments)

    def _budget(
        self,
        conversation: tuple[ConversationEntry, ...],
        request: ModelRequest,
    ):
        footprint = self.meter.footprint(request)
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        anchor = _usage_anchor(conversation, binding)
        if anchor is None:
            base = None
            delta = self.meter.estimate(binding, footprint).tokens
            projected = delta
            measurement = "estimated"
        else:
            assert anchor.context_footprint is not None
            base = anchor.usage.total_input_tokens + anchor.usage.output_tokens
            current_estimate = self.meter.estimate(binding, footprint).tokens
            anchor_estimate = self.meter.estimate(
                binding, anchor.context_footprint
            ).tokens
            delta = current_estimate - anchor_estimate
            projected = max(1, base + delta)
            measurement = "reported"
        environment = self._model_environment()
        input_limit = (
            environment.descriptor.limits.effective_input_limit(self.max_output_tokens)
            or FALLBACK_INPUT_TOKENS
        )
        budget = ContextBudget(
            reported_base_tokens=base,
            estimated_delta_tokens=delta,
            projected_tokens=projected,
            reserved_output_tokens=self.max_output_tokens,
            input_limit_tokens=input_limit,
            compact_trigger_tokens=environment.compact_trigger_tokens,
            measurement=measurement,
            model_limits=environment.descriptor.limits,
            model_limit_source=environment.descriptor.source,
            configured_compact_trigger_tokens=(
                environment.configured_compact_trigger_tokens
            ),
            warning=environment.warning or environment.discovery_error,
        )
        return budget, footprint

    def _projected_tokens_for(
        self,
        conversation: tuple[ConversationEntry, ...],
        user_context: tuple[UserContextDocument, ...],
        replay_records: tuple[ProviderReplayRecord, ...],
        prompt: SystemPrompt,
        tools: tuple[ModelToolDefinition, ...],
    ) -> int:
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        messages = self.normalizer.normalize(
            user_context,
            conversation,
            replay_records,
            active_binding=binding,
        )
        request = ModelRequest(prompt, messages, tools, self.max_output_tokens)
        budget, _ = self._budget(conversation, request)
        return budget.projected_tokens


def _usage_anchor(
    conversation: tuple[ConversationEntry, ...],
    binding: ProviderBinding | None,
) -> AssistantMessage | None:
    if binding is None:
        return None
    return next(
        (
            message
            for message in reversed(conversation)
            if isinstance(message, AssistantMessage)
            and message.provider_binding == binding
            and message.context_footprint is not None
            and message.usage.provider_reported
        ),
        None,
    )


def _request_provenance(
    user_context: tuple[UserContextDocument, ...],
    conversation: tuple[ConversationEntry, ...],
    replacements: tuple[ContentReplacement, ...],
) -> tuple[ModelInputOrigin, ...]:
    """Build a stable one-to-one origin projection beside normalization."""

    replaced_tool_ids = {replacement.tool_use_id for replacement in replacements}
    origins = [
        ModelInputOrigin(
            ModelInputOriginKind.USER_CONTEXT,
            source=document.source,
        )
        for document in user_context
    ]
    for entry in conversation:
        if isinstance(entry, ToolResultBatch) and any(
            result.tool_use_id in replaced_tool_ids for result in entry.content
        ):
            kind = ModelInputOriginKind.CONTENT_REPLACEMENT
        elif isinstance(entry, HumanMessage):
            kind = ModelInputOriginKind.USER_MESSAGE
        elif isinstance(entry, AttachmentMessage):
            kind = ModelInputOriginKind.ATTACHMENT
        elif isinstance(entry, ConversationSummaryMessage):
            kind = ModelInputOriginKind.SUMMARY
        else:
            kind = ModelInputOriginKind.CONVERSATION_ENTRY
        origins.append(
            ModelInputOrigin(
                kind,
                source_id=entry.uuid,
                attachment_kind=(
                    entry.payload.kind if isinstance(entry, AttachmentMessage) else None
                ),
            )
        )
    return tuple(origins)


__all__ = [
    "ContextPlanner",
]
