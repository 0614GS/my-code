"""从会话快照生成完整 ModelRequest。"""

import json
from collections.abc import Callable
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from my_code.context.attachments.models import ContextAttachment
from my_code.context.attachments.sources import DerivedAttachmentResolver
from my_code.context.documents import UserContextDocument
from my_code.context.microcompact import (
    MicrocompactPolicy,
    apply_content_replacements,
)
from my_code.context.models import ContextBudget, ContextOverflow, ContextPlan
from my_code.context.normalization import ModelInputNormalizer
from my_code.context.session import (
    AttachmentDelivery,
    ContextSnapshot,
    SessionContextAccess,
)
from my_code.context.tokenizer import UnicodeTokenEstimator
from my_code.context.user_context import EmptyUserContextResolver, UserContextResolver
from my_code.context.window import ContextWindow
from my_code.context.xml import render_context_instruction
from my_code.conversation.models import (
    AssistantMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResultBatch,
)
from my_code.conversation.state import ContentReplacement
from my_code.model.capabilities import (
    FALLBACK_INPUT_TOKENS,
    ActiveModelEnvironment,
    fallback_descriptor,
    resolve_environment,
)
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ProviderReplayRecord,
)
from my_code.model.request import (
    AssistantOutput,
    InputText,
    ModelInputItem,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolDefinition,
    ModelToolUseBlock,
    SystemPrompt,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)
from my_code.prompts.registry import PromptRegistry


class ContextPlanner:
    """集中拥有 ConversationEntry → ModelInputItem 投影边界。"""

    def __init__(
        self,
        *,
        window: ContextWindow,
        prompt: PromptRegistry,
        tools: tuple[ModelToolDefinition, ...],
        max_output_tokens: int,
        normalizer: ModelInputNormalizer | None = None,
        microcompact: MicrocompactPolicy | None = None,
        user_context_resolver: UserContextResolver | None = None,
        attachment_resolver: DerivedAttachmentResolver | None = None,
        binding_resolver: Callable[[], ProviderBinding] | None = None,
        model_environment: Callable[[], ActiveModelEnvironment] | None = None,
        token_estimator: UnicodeTokenEstimator | None = None,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.window = window
        self.prompt = prompt
        self.tools = tools
        self.max_output_tokens = max_output_tokens
        self.normalizer = normalizer or ModelInputNormalizer()
        self.microcompact = microcompact or MicrocompactPolicy.for_window(
            window.max_chars
        )
        self.user_context_resolver = user_context_resolver or EmptyUserContextResolver()
        self.attachment_resolver = attachment_resolver or DerivedAttachmentResolver()
        self.binding_resolver = binding_resolver
        fallback_environment = resolve_environment(
            fallback_descriptor("unknown"),
            requested_output_tokens=max_output_tokens,
            configured_trigger_tokens=None,
        )
        self._model_environment = model_environment or (lambda: fallback_environment)
        self.token_estimator = token_estimator or UnicodeTokenEstimator()
        self.attachment_projector = self.normalizer.attachment_projector

    def plan(
        self,
        snapshot: ContextSnapshot,
        session: SessionContextAccess | None = None,
    ) -> ContextPlan:
        effective, proposed = self._effective_messages(snapshot)
        user_context = self._get_user_context(session)
        attachments = self._get_attachments(snapshot)
        delivered = tuple(
            delivery.attachment for delivery in snapshot.attachment_deliveries
        )
        attachment_chars = self.attachment_projector.measure(delivered + attachments)
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        selected = self.window.ensure_fits(
            effective,
            additional_chars=attachment_chars
            + _replayed_continuation_chars(effective, snapshot.replay_records, binding),
        )
        model_messages = self.normalizer.normalize(
            user_context,
            selected,
            attachments,
            snapshot.attachment_deliveries,
            snapshot.replay_records,
            active_binding=binding,
        )
        system_prompt = (
            session.resolve_prompt(self.prompt)
            if session is not None
            else self.prompt.resolve()
        )
        request = ModelRequest(
            system_prompt, model_messages, self.tools, self.max_output_tokens
        )
        budget, local_estimate = self._budget(
            selected,
            request,
            user_context,
            attachments,
            snapshot.attachment_deliveries,
        )
        if budget.input_tokens >= budget.compact_trigger_tokens:
            token_proposed = self.microcompact.propose_tokens(
                snapshot.messages,
                snapshot.content_replacements + proposed,
                current_tokens=budget.input_tokens,
                trigger_tokens=budget.compact_trigger_tokens,
                estimate=lambda view: self._projected_tokens_for(
                    view,
                    user_context,
                    attachments,
                    snapshot.attachment_deliveries,
                    snapshot.replay_records,
                    system_prompt,
                ),
            )
            if token_proposed:
                proposed += token_proposed
                selected = apply_content_replacements(
                    snapshot.messages,
                    snapshot.content_replacements + proposed,
                )
                model_messages = self.normalizer.normalize(
                    user_context,
                    selected,
                    attachments,
                    snapshot.attachment_deliveries,
                    snapshot.replay_records,
                    active_binding=binding,
                )
                request = ModelRequest(
                    system_prompt, model_messages, self.tools, self.max_output_tokens
                )
                budget, local_estimate = self._budget(
                    selected,
                    request,
                    user_context,
                    attachments,
                    snapshot.attachment_deliveries,
                )
        if budget.input_tokens >= budget.compact_trigger_tokens:
            raise ContextOverflow(budget.input_tokens, budget.input_limit_tokens)
        return ContextPlan(
            request=request,
            budget=budget,
            new_content_replacements=proposed,
            new_attachment_deliveries=self._new_deliveries(snapshot, attachments),
            request_binding=binding,
            request_input_tokens_estimate=local_estimate,
        )

    def inspect(
        self,
        snapshot: ContextSnapshot,
        session: SessionContextAccess | None = None,
    ) -> ContextBudget:
        effective, _ = self._effective_messages(snapshot, propose=False)
        user_context = self._get_user_context(session)
        attachments = self._get_attachments(snapshot)
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        messages = self.normalizer.normalize(
            user_context,
            effective,
            attachments,
            snapshot.attachment_deliveries,
            snapshot.replay_records,
            active_binding=binding,
        )
        request = ModelRequest(
            (
                session.resolve_prompt(self.prompt)
                if session is not None
                else self.prompt.resolve()
            ),
            messages,
            self.tools,
            self.max_output_tokens,
        )
        budget, _ = self._budget(
            effective,
            request,
            user_context,
            attachments,
            snapshot.attachment_deliveries,
        )
        return budget

    def compaction_view(
        self, snapshot: ContextSnapshot
    ) -> tuple[tuple[ModelInputItem, ...], tuple[ContentReplacement, ...]]:
        effective, proposed = self._effective_messages(snapshot)
        return (
            self.normalizer.normalize_transcript(
                effective, snapshot.attachment_deliveries
            ),
            proposed,
        )

    def measure(self, messages: tuple[ConversationEntry, ...]) -> int:
        return self.window.size(messages)

    def _get_user_context(
        self, session: SessionContextAccess | None
    ) -> tuple[UserContextDocument, ...]:
        if session is None:
            return tuple(self.user_context_resolver.resolve())
        return session.user_context(self.user_context_resolver.resolve)

    def _get_attachments(
        self, snapshot: ContextSnapshot
    ) -> tuple[ContextAttachment, ...]:
        return self.attachment_resolver.resolve(snapshot)

    @staticmethod
    def _new_deliveries(
        snapshot: ContextSnapshot,
        attachments: tuple[ContextAttachment, ...],
    ) -> tuple[AttachmentDelivery, ...]:
        live = tuple(
            attachment
            for attachment in attachments
            if attachment.retention == "live_session"
        )
        if not live:
            return ()
        if not snapshot.messages:
            raise ValueError("Live-session attachments require a working-set anchor")
        anchor_uuid = snapshot.messages[-1].uuid
        return tuple(
            AttachmentDelivery(
                anchor_uuid,
                attachment,
                delivery_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"my-code:{anchor_uuid}:{index}:{attachment!r}",
                    )
                ),
            )
            for index, attachment in enumerate(live)
        )

    def _effective_messages(
        self, snapshot: ContextSnapshot, *, propose: bool = True
    ) -> tuple[tuple[ConversationEntry, ...], tuple[ContentReplacement, ...]]:
        proposed = (
            self.microcompact.propose(snapshot.messages, snapshot.content_replacements)
            if propose
            else ()
        )
        return apply_content_replacements(
            snapshot.messages, snapshot.content_replacements + proposed
        ), proposed

    def _budget(
        self,
        conversation: tuple[ConversationEntry, ...],
        request: ModelRequest,
        user_context: tuple[UserContextDocument, ...],
        attachments: tuple[ContextAttachment, ...],
        deliveries: tuple[AttachmentDelivery, ...],
    ) -> tuple[ContextBudget, int]:
        user_chars = _context_chars(user_context)
        delivered = tuple(delivery.attachment for delivery in deliveries)
        attachment_chars = self.attachment_projector.measure(delivered + attachments)
        local_estimate = self.token_estimator.count_request(request)
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        anchor = _usage_anchor(conversation, binding)
        if anchor is None:
            actual = None
            projected = local_estimate
            measurement: Literal["reported_calibrated", "tokenizer_estimate"] = (
                "tokenizer_estimate"
            )
        else:
            actual = anchor.usage.total_input_tokens
            projected = max(
                1,
                actual
                + local_estimate
                - (anchor.request_input_tokens_estimate or local_estimate),
            )
            measurement = "reported_calibrated"
        environment = self._model_environment()
        input_limit = (
            environment.descriptor.limits.effective_input_limit(self.max_output_tokens)
            or FALLBACK_INPUT_TOKENS
        )
        budget = ContextBudget(
            message_limit_chars=self.window.max_chars,
            message_chars=_message_chars(request.input) - user_chars - attachment_chars,
            system_chars=len(request.system_prompt.text),
            tool_schema_chars=_tool_schema_chars(self.tools),
            reserved_output_tokens=self.max_output_tokens,
            last_actual_input_tokens=actual,
            incremental_tokens=(projected if actual is None else projected - actual),
            estimated_input_tokens=projected,
            user_context_chars=user_chars,
            attachment_chars=attachment_chars,
            input_tokens=projected,
            input_limit_tokens=input_limit,
            compact_trigger_tokens=environment.compact_trigger_tokens,
            last_reported_input_tokens=actual,
            measurement=measurement,
            model_limits=environment.descriptor.limits,
            model_limit_source=environment.descriptor.source,
            configured_compact_trigger_tokens=(
                environment.configured_compact_trigger_tokens
            ),
            warning=environment.warning or environment.discovery_error,
        )
        return budget, local_estimate

    def _projected_tokens_for(
        self,
        conversation: tuple[ConversationEntry, ...],
        user_context: tuple[UserContextDocument, ...],
        attachments: tuple[ContextAttachment, ...],
        deliveries: tuple[AttachmentDelivery, ...],
        replay_records: tuple[ProviderReplayRecord, ...],
        prompt: SystemPrompt,
    ) -> int:
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        messages = self.normalizer.normalize(
            user_context,
            conversation,
            attachments,
            deliveries,
            replay_records,
            active_binding=binding,
        )
        request = ModelRequest(prompt, messages, self.tools, self.max_output_tokens)
        budget, _ = self._budget(
            conversation, request, user_context, attachments, deliveries
        )
        return budget.input_tokens


def _message_chars(items: tuple[ModelInputItem, ...]) -> int:
    size = 0
    for item in items:
        if isinstance(item, UserInput):
            for block in item.content:
                if isinstance(block, InputText):
                    size += len(block.text)
                else:
                    size += len(block.data)
        elif isinstance(item, AssistantOutput):
            for block in item.content:
                if isinstance(block, ModelTextBlock):
                    size += (
                        _continuation_chars(block.continuation)
                        if block.continuation is not None
                        else len(block.text)
                    )
                elif isinstance(block, ModelToolUseBlock):
                    size += (
                        _continuation_chars(block.continuation)
                        if block.continuation is not None
                        else len(block.name) + len(str(block.input))
                    )
                elif isinstance(block, ModelReasoningBlock):
                    if block.continuation is not None:
                        size += _continuation_chars(block.continuation)
        elif isinstance(item, ToolOutputs):
            for output in item.results:
                size += sum(
                    len(block.text)
                    if isinstance(block, ToolOutputText)
                    else len(block.data)
                    for block in output.content
                )
    return size


def _context_chars(items: tuple[UserContextDocument, ...]) -> int:
    return sum(
        len(
            block.text
            if isinstance(block, TextContent)
            else render_context_instruction(block)
        )
        for item in items
        for block in item.content
    )


def _deliveries_after_last_assistant(
    conversation: tuple[ConversationEntry, ...],
    deliveries: tuple[AttachmentDelivery, ...],
) -> tuple[ContextAttachment, ...]:
    last_assistant = next(
        (
            index
            for index in range(len(conversation) - 1, -1, -1)
            if isinstance(conversation[index], AssistantMessage)
        ),
        -1,
    )
    positions = {message.uuid: index for index, message in enumerate(conversation)}
    return tuple(
        delivery.attachment
        for delivery in deliveries
        if positions.get(delivery.anchor_uuid, -1) >= last_assistant
    )


def _replayed_continuation_chars(
    conversation: tuple[ConversationEntry, ...],
    replay_records: tuple[ProviderReplayRecord, ...],
    binding: ProviderBinding | None,
) -> int:
    source_uuid = (
        conversation[-1].source_assistant_id
        if conversation and isinstance(conversation[-1], ToolResultBatch)
        else None
    )
    by_id = {message.uuid: message for message in conversation}
    size = 0
    for record in replay_records:
        message = by_id.get(record.entry_id)
        if not isinstance(message, AssistantMessage):
            continue
        try:
            index = int(record.content_id.removeprefix("content:"))
            block = message.content[index]
        except (ValueError, IndexError):
            continue
        continuation = record.state
        if binding is not None and continuation.binding != binding:
            continue
        if (
            continuation.replay_scope != "working_context"
            and message.uuid != source_uuid
        ):
            continue
        size += max(
            0,
            _continuation_chars(continuation) - _fallback_block_chars(block),
        )
    return size


def _fallback_block_chars(block: object) -> int:
    if isinstance(block, TextContent):
        return len(block.text)
    if isinstance(block, ToolCall):
        return len(block.name) + len(str(block.input))
    return 0


def _tool_schema_chars(tools: tuple[ModelToolDefinition, ...]) -> int:
    return sum(
        len(
            json.dumps(
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        for t in tools
    )


def _conversation_chars(messages: tuple[ConversationEntry, ...]) -> int:
    size = 0
    for message in messages:
        if isinstance(message, (HumanMessage, ConversationSummaryMessage)):
            size += len(message.content)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                size += (
                    len(block.text)
                    if isinstance(block, TextContent)
                    else len(block.name) + len(str(block.input))
                    if isinstance(block, ToolCall)
                    else 0
                )
        else:
            size += sum(len(result.content) for result in message.content)
    return size


def _estimate(
    conversation: tuple[ConversationEntry, ...],
    model: tuple[ModelInputItem, ...],
    system: str,
    tools: tuple[ModelToolDefinition, ...],
    attachment_chars: int,
) -> tuple[int | None, int, int]:
    for index in range(len(conversation) - 1, -1, -1):
        message = conversation[index]
        if not isinstance(message, AssistantMessage):
            continue
        incremental = _chars_to_tokens(
            _conversation_chars(conversation[index + 1 :]) + attachment_chars
        )
        actual = message.usage.total_input_tokens
        return actual, incremental, actual + message.usage.output_tokens + incremental
    estimated = _chars_to_tokens(
        len(system) + _tool_schema_chars(tools) + _message_chars(model)
    )
    return None, estimated, estimated


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
            and message.request_input_tokens_estimate is not None
            and message.usage.provider_reported
        ),
        None,
    )


def _chars_to_tokens(chars: int) -> int:
    return (chars + 3) // 4


def _continuation_chars(state: ProviderContinuationState) -> int:
    return len(
        json.dumps(
            state.payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


__all__ = [
    "ContextPlanner",
]
