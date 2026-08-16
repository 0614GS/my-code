"""从会话快照生成完整 ModelRequest。"""

import json

from nano_code.agent.contracts.context import ContextBudget, ContextPlan
from nano_code.agent.contracts.model import (
    ModelMessage,
    ModelRequest,
    ModelTextBlock,
    ModelToolDefinition,
    ModelToolUseBlock,
)
from nano_code.agent.contracts.session import ContentReplacement, ConversationSnapshot
from nano_code.agent.ports.context import ContextPort
from nano_code.context.attachments import AttachmentResolver
from nano_code.context.microcompact import (
    MicrocompactPolicy,
    apply_content_replacements,
)
from nano_code.context.normalization import ModelInputNormalizer
from nano_code.context.user_context import EmptyUserContextResolver, UserContextResolver
from nano_code.context.window import ContextWindow
from nano_code.messages import (
    AssistantMessage,
    ContextAttachment,
    ConversationMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    UserContextDocument,
)
from nano_code.messages.xml import render_context_instruction
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
        attachment_resolver: AttachmentResolver | None = None,
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
        self.attachment_resolver = attachment_resolver or AttachmentResolver()
        self._user_context_cache: tuple[UserContextDocument, ...] | None = None

    def plan(self, snapshot: ConversationSnapshot) -> ContextPlan:
        effective, proposed = self._effective_messages(snapshot)
        selected = self.window.ensure_fits(effective)
        user_context = self._get_user_context()
        attachments = self._get_attachments(snapshot)
        model_messages = self.normalizer.normalize(user_context, selected, attachments)
        system_prompt = self.prompt.resolve()
        budget = self._budget(
            selected, model_messages, user_context, attachments, system_prompt
        )
        return ContextPlan(
            request=ModelRequest(
                system_prompt, model_messages, self.tools, self.max_output_tokens
            ),
            budget=budget,
            new_content_replacements=proposed,
        )

    def inspect(self, snapshot: ConversationSnapshot) -> ContextBudget:
        effective, _ = self._effective_messages(snapshot, propose=False)
        user_context = self._get_user_context()
        attachments = self._get_attachments(snapshot)
        messages = self.normalizer.normalize(user_context, effective, attachments)
        return self._budget(
            effective, messages, user_context, attachments, self.prompt.resolve()
        )

    def compaction_view(
        self, snapshot: ConversationSnapshot
    ) -> tuple[tuple[ModelMessage, ...], tuple[ContentReplacement, ...]]:
        effective, proposed = self._effective_messages(snapshot)
        return self.normalizer.normalize_transcript(effective), proposed

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
        prompt: SystemPrompt,
    ) -> ContextBudget:
        user_chars = _context_chars(user_context)
        attachment_chars = _context_chars(attachments)
        actual, incremental, estimated = _estimate(
            conversation, messages, prompt.text, self.tools, attachment_chars
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
                size += len(block.text)
            elif isinstance(block, ModelToolUseBlock):
                size += len(block.name) + len(str(block.input))
            else:
                size += len(block.content)
    return size


def _context_chars(
    items: tuple[UserContextDocument, ...] | tuple[ContextAttachment, ...],
) -> int:
    return sum(
        len(
            block.text
            if isinstance(block, TextContent)
            else render_context_instruction(block)
        )
        for item in items
        for block in item.content
    )


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
