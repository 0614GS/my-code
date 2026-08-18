"""从会话快照生成完整 ModelRequest。"""

import json
from collections.abc import Callable

from nano_code.agent.contracts.context import ContextBudget, ContextPlan
from nano_code.agent.contracts.model import (
    ModelMessage,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolDefinition,
    ModelToolUseBlock,
)
from nano_code.agent.contracts.session import (
    AttachmentDelivery,
    ContentReplacement,
    ConversationSnapshot,
)
from nano_code.agent.ports.context import ContextPort
from nano_code.context.attachments.models import ContextAttachment
from nano_code.context.attachments.sources import DerivedAttachmentResolver
from nano_code.context.documents import UserContextDocument
from nano_code.context.microcompact import (
    MicrocompactPolicy,
    apply_content_replacements,
)
from nano_code.context.normalization import ModelInputNormalizer
from nano_code.context.user_context import EmptyUserContextResolver, UserContextResolver
from nano_code.context.window import ContextWindow
from nano_code.context.xml import render_context_instruction
from nano_code.conversation import (
    AssistantMessage,
    ConversationMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ProviderBinding,
    ProviderContinuationState,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultsMessage,
)
from nano_code.prompts import PromptRegistry, SystemPrompt


class ContextPlanner(ContextPort):
    """集中拥有 ConversationMessage → ModelMessage 投影边界。"""

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
        self.attachment_projector = self.normalizer.attachment_projector
        self._user_context_cache: tuple[UserContextDocument, ...] | None = None

    def plan(self, snapshot: ConversationSnapshot) -> ContextPlan:
        effective, proposed = self._effective_messages(snapshot)
        user_context = self._get_user_context()
        attachments = self._get_attachments(snapshot)
        delivered = tuple(
            delivery.attachment for delivery in snapshot.attachment_deliveries
        )
        attachment_chars = self.attachment_projector.measure(delivered + attachments)
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        selected = self.window.ensure_fits(
            effective,
            additional_chars=attachment_chars
            + _replayed_continuation_chars(effective, binding),
        )
        model_messages = self.normalizer.normalize(
            user_context,
            selected,
            attachments,
            snapshot.attachment_deliveries,
            active_binding=binding,
        )
        system_prompt = self.prompt.resolve()
        budget = self._budget(
            selected,
            model_messages,
            user_context,
            attachments,
            snapshot.attachment_deliveries,
            system_prompt,
        )
        return ContextPlan(
            request=ModelRequest(
                system_prompt, model_messages, self.tools, self.max_output_tokens
            ),
            budget=budget,
            new_content_replacements=proposed,
            new_attachment_deliveries=self._new_deliveries(snapshot, attachments),
        )

    def inspect(self, snapshot: ConversationSnapshot) -> ContextBudget:
        effective, _ = self._effective_messages(snapshot, propose=False)
        user_context = self._get_user_context()
        attachments = self._get_attachments(snapshot)
        binding = self.binding_resolver() if self.binding_resolver is not None else None
        messages = self.normalizer.normalize(
            user_context,
            effective,
            attachments,
            snapshot.attachment_deliveries,
            active_binding=binding,
        )
        return self._budget(
            effective,
            messages,
            user_context,
            attachments,
            snapshot.attachment_deliveries,
            self.prompt.resolve(),
        )

    def compaction_view(
        self, snapshot: ConversationSnapshot
    ) -> tuple[tuple[ModelMessage, ...], tuple[ContentReplacement, ...]]:
        effective, proposed = self._effective_messages(snapshot)
        return (
            self.normalizer.normalize_transcript(
                effective, snapshot.attachment_deliveries
            ),
            proposed,
        )

    def measure(self, messages: tuple[ConversationMessage, ...]) -> int:
        return self.window.size(messages)

    def _get_user_context(self) -> tuple[UserContextDocument, ...]:
        if self._user_context_cache is None:
            self._user_context_cache = tuple(self.user_context_resolver.resolve())
        return self._user_context_cache

    def _get_attachments(
        self, snapshot: ConversationSnapshot
    ) -> tuple[ContextAttachment, ...]:
        return self.attachment_resolver.resolve(snapshot)

    @staticmethod
    def _new_deliveries(
        snapshot: ConversationSnapshot,
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
        return tuple(AttachmentDelivery(anchor_uuid, attachment) for attachment in live)

    def _effective_messages(
        self, snapshot: ConversationSnapshot, *, propose: bool = True
    ) -> tuple[tuple[ConversationMessage, ...], tuple[ContentReplacement, ...]]:
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
        conversation: tuple[ConversationMessage, ...],
        messages: tuple[ModelMessage, ...],
        user_context: tuple[UserContextDocument, ...],
        attachments: tuple[ContextAttachment, ...],
        deliveries: tuple[AttachmentDelivery, ...],
        prompt: SystemPrompt,
    ) -> ContextBudget:
        user_chars = _context_chars(user_context)
        delivered = tuple(delivery.attachment for delivery in deliveries)
        attachment_chars = self.attachment_projector.measure(delivered + attachments)
        incremental_attachment_chars = self.attachment_projector.measure(
            attachments + _deliveries_after_last_assistant(conversation, deliveries)
        )
        actual, incremental, estimated = _estimate(
            conversation,
            messages,
            prompt.text,
            self.tools,
            incremental_attachment_chars,
        )
        return ContextBudget(
            message_limit_chars=self.window.max_chars,
            message_chars=_message_chars(messages) - user_chars - attachment_chars,
            system_chars=len(prompt.text),
            tool_schema_chars=_tool_schema_chars(self.tools),
            reserved_output_tokens=self.max_output_tokens,
            last_actual_input_tokens=actual,
            incremental_tokens=incremental,
            estimated_input_tokens=estimated,
            user_context_chars=user_chars,
            attachment_chars=attachment_chars,
        )


def _message_chars(messages: tuple[ModelMessage, ...]) -> int:
    size = 0
    for message in messages:
        for block in message.content:
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
            else:
                size += len(block.content)
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
    conversation: tuple[ConversationMessage, ...],
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
    conversation: tuple[ConversationMessage, ...],
    binding: ProviderBinding | None,
) -> int:
    source_uuid = (
        conversation[-1].source_assistant_uuid
        if conversation and isinstance(conversation[-1], ToolResultsMessage)
        else None
    )
    return sum(
        max(
            0,
            _continuation_chars(continuation) - _fallback_block_chars(block),
        )
        for message in conversation
        if isinstance(message, AssistantMessage)
        for block in message.content
        for continuation in (_block_continuation(block),)
        if continuation is not None
        and (binding is None or continuation.binding == binding)
        and (
            continuation.replay_scope == "working_context"
            or message.uuid == source_uuid
        )
    )


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


def _conversation_chars(messages: tuple[ConversationMessage, ...]) -> int:
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
    conversation: tuple[ConversationMessage, ...],
    model: tuple[ModelMessage, ...],
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


def _block_continuation(block: object) -> ProviderContinuationState | None:
    if isinstance(block, (TextContent, ToolCall, ReasoningContent)):
        return block.continuation
    return None
