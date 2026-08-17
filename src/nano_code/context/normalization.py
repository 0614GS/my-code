"""ConversationMessage 到 ModelMessage 的纯投影与协议校验。"""

from nano_code.agent.contracts.model import (
    ModelAssistantMessage,
    ModelMessage,
    ModelTextBlock,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
)
from nano_code.agent.contracts.session import AttachmentDelivery
from nano_code.messages import (
    AssistantMessage,
    ContextAttachment,
    ConversationMessage,
    HumanMessage,
    TextContent,
    ToolResultsMessage,
    UserContextDocument,
)
from nano_code.messages.xml import render_context_instruction, wrap_xml

from .attachment_projection import AttachmentProjector


class ModelInputNormalizer:
    """唯一了解 conversation 与 model 两层消息的映射器。"""

    def __init__(self, attachment_projector: AttachmentProjector | None = None) -> None:
        self.attachment_projector = attachment_projector or AttachmentProjector()

    def normalize(
        self,
        user_context: tuple[UserContextDocument, ...],
        history: tuple[ConversationMessage, ...],
        attachments: tuple[ContextAttachment, ...],
        attachment_deliveries: tuple[AttachmentDelivery, ...] = (),
    ) -> tuple[ModelMessage, ...]:
        candidates = [
            *(_context_message(item) for item in user_context),
            *_conversation_history(
                history, attachment_deliveries, self.attachment_projector
            ),
            *self.attachment_projector.project_many(attachments),
        ]
        result = _merge_adjacent(tuple(candidates))
        self._validate_tool_pairs(result)
        return result

    def normalize_transcript(
        self,
        messages: tuple[ConversationMessage, ...],
        attachment_deliveries: tuple[AttachmentDelivery, ...] = (),
    ) -> tuple[ModelMessage, ...]:
        """Compact 使用的 history-only 视图。"""

        result = _merge_adjacent(
            tuple(
                _conversation_history(
                    messages, attachment_deliveries, self.attachment_projector
                )
            )
        )
        self._validate_tool_pairs(result)
        return result

    @staticmethod
    def _validate_tool_pairs(messages: tuple[ModelMessage, ...]) -> None:
        pending: set[str] = set()
        seen_calls: set[str] = set()
        seen_results: set[str] = set()
        for message in messages:
            for block in message.content:
                if isinstance(block, ModelToolUseBlock):
                    if block.id in seen_calls:
                        raise ValueError(f"Duplicate tool use in context: {block.id}")
                    seen_calls.add(block.id)
                    pending.add(block.id)
                elif isinstance(block, ModelToolResultBlock):
                    if block.tool_use_id in seen_results:
                        raise ValueError(
                            f"Duplicate tool result in context: {block.tool_use_id}"
                        )
                    if block.tool_use_id not in pending:
                        raise ValueError(
                            f"Orphan tool result in context: {block.tool_use_id}"
                        )
                    seen_results.add(block.tool_use_id)
                    pending.remove(block.tool_use_id)
        if pending:
            raise ValueError(
                f"Unresolved tool use in context: {', '.join(sorted(pending))}"
            )


def _conversation_message(message: ConversationMessage) -> ModelMessage:
    if isinstance(message, HumanMessage):
        return ModelUserMessage((ModelTextBlock(message.content),))
    if isinstance(message, AssistantMessage):
        return ModelAssistantMessage(
            tuple(
                ModelTextBlock(block.text)
                if isinstance(block, TextContent)
                else ModelToolUseBlock(block.id, block.name, block.input)
                for block in message.content
            )
        )
    if isinstance(message, ToolResultsMessage):
        return ModelUserMessage(
            tuple(
                ModelToolResultBlock(
                    result.tool_use_id, result.content, result.is_error
                )
                for result in message.content
            )
        )
    return ModelUserMessage(
        (ModelTextBlock(wrap_xml("conversation-summary", message.content)),)
    )


def _context_message(message: UserContextDocument) -> ModelUserMessage:
    return ModelUserMessage(
        tuple(
            ModelTextBlock(
                block.text
                if isinstance(block, TextContent)
                else render_context_instruction(block)
            )
            for block in message.content
        )
    )


def _conversation_history(
    messages: tuple[ConversationMessage, ...],
    attachment_deliveries: tuple[AttachmentDelivery, ...],
    attachment_projector: AttachmentProjector,
) -> list[ModelMessage]:
    by_anchor: dict[str, list[ContextAttachment]] = {}
    for delivery in attachment_deliveries:
        by_anchor.setdefault(delivery.anchor_uuid, []).append(delivery.attachment)
    projected: list[ModelMessage] = []
    for message in messages:
        projected.append(_conversation_message(message))
        projected.extend(
            attachment_projector.project_many(tuple(by_anchor.get(message.uuid, ())))
        )
    return projected


def _merge_adjacent(messages: tuple[ModelMessage, ...]) -> tuple[ModelMessage, ...]:
    merged: list[ModelMessage] = []
    for message in messages:
        if (
            merged
            and isinstance(merged[-1], ModelUserMessage)
            and isinstance(message, ModelUserMessage)
        ):
            merged[-1] = ModelUserMessage(merged[-1].content + message.content)
        elif (
            merged
            and isinstance(merged[-1], ModelAssistantMessage)
            and isinstance(message, ModelAssistantMessage)
        ):
            merged[-1] = ModelAssistantMessage(merged[-1].content + message.content)
        else:
            merged.append(message)
    return tuple(merged)
