"""ConversationEntry 到 ModelInputItem 的纯投影与协议校验。"""

from my_code.context.attachments.models import ContextAttachment
from my_code.context.attachments.projection import AttachmentProjector
from my_code.context.documents import UserContextDocument
from my_code.context.session import AttachmentDelivery
from my_code.context.xml import render_context_instruction, wrap_xml
from my_code.conversation.models import (
    AssistantMessage,
    ConversationEntry,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultBatch,
)
from my_code.model.primitives import ProviderBinding, ProviderContinuationState
from my_code.model.request import (
    AssistantOutput,
    InputText,
    ModelInputItem,
    ModelReasoningBlock,
    ModelTextBlock,
    ModelToolUseBlock,
    ToolOutput,
    ToolOutputs,
    ToolOutputText,
    UserInput,
    validate_model_input,
)


class ModelInputNormalizer:
    """唯一了解 conversation facts 与 model input items 的映射器。"""

    def __init__(self, attachment_projector: AttachmentProjector | None = None) -> None:
        self.attachment_projector = attachment_projector or AttachmentProjector()

    def normalize(
        self,
        user_context: tuple[UserContextDocument, ...],
        history: tuple[ConversationEntry, ...],
        attachments: tuple[ContextAttachment, ...],
        attachment_deliveries: tuple[AttachmentDelivery, ...] = (),
        active_binding: ProviderBinding | None = None,
    ) -> tuple[ModelInputItem, ...]:
        candidates = [
            *(_context_message(item) for item in user_context),
            *_conversation_history(
                history,
                attachment_deliveries,
                self.attachment_projector,
                replay_continuation=True,
                active_binding=active_binding,
            ),
            *self.attachment_projector.project_many(attachments),
        ]
        result = tuple(candidates)
        validate_model_input(result)
        return result

    def normalize_transcript(
        self,
        messages: tuple[ConversationEntry, ...],
        attachment_deliveries: tuple[AttachmentDelivery, ...] = (),
    ) -> tuple[ModelInputItem, ...]:
        """Compact 使用的 history-only 视图。"""

        result = tuple(
            _conversation_history(
                messages,
                attachment_deliveries,
                self.attachment_projector,
                replay_continuation=False,
            )
        )
        validate_model_input(result)
        return result


def _conversation_message(
    message: ConversationEntry,
    *,
    active_trajectory: bool = False,
    replay_continuation: bool = True,
    active_binding: ProviderBinding | None = None,
) -> ModelInputItem:
    if isinstance(message, HumanMessage):
        return UserInput((InputText(message.content),))
    if isinstance(message, AssistantMessage):
        return AssistantOutput(
            tuple(
                ModelTextBlock(
                    block.text,
                    _selected_continuation(
                        block.continuation,
                        active_trajectory,
                        replay_continuation,
                        active_binding,
                    ),
                )
                if isinstance(block, TextContent)
                else ModelToolUseBlock(
                    block.id,
                    block.name,
                    block.input,
                    _selected_continuation(
                        block.continuation,
                        active_trajectory,
                        replay_continuation,
                        active_binding,
                    ),
                )
                if isinstance(block, ToolCall)
                else ModelReasoningBlock(
                    block.id,
                    block.presentation,
                    _selected_continuation(
                        block.continuation,
                        active_trajectory,
                        replay_continuation,
                        active_binding,
                    ),
                )
                for block in message.content
                if not isinstance(block, ReasoningContent)
                or _selected_continuation(
                    block.continuation,
                    active_trajectory,
                    replay_continuation,
                    active_binding,
                )
                is not None
            )
        )
    if isinstance(message, ToolResultBatch):
        return ToolOutputs(
            tuple(
                ToolOutput(
                    result.tool_use_id,
                    (ToolOutputText(result.content),),
                    result.is_error,
                )
                for result in message.content
            )
        )
    return UserInput((InputText(wrap_xml("conversation-summary", message.content)),))


def _context_message(message: UserContextDocument) -> UserInput:
    return UserInput(
        tuple(
            InputText(
                block.text
                if isinstance(block, TextContent)
                else render_context_instruction(block)
            )
            for block in message.content
        )
    )


def _conversation_history(
    messages: tuple[ConversationEntry, ...],
    attachment_deliveries: tuple[AttachmentDelivery, ...],
    attachment_projector: AttachmentProjector,
    *,
    replay_continuation: bool,
    active_binding: ProviderBinding | None = None,
) -> list[ModelInputItem]:
    by_anchor: dict[str, list[ContextAttachment]] = {}
    for delivery in attachment_deliveries:
        by_anchor.setdefault(delivery.anchor_uuid, []).append(delivery.attachment)
    active_trajectory_uuid = (
        messages[-1].source_assistant_id
        if replay_continuation
        and messages
        and isinstance(messages[-1], ToolResultBatch)
        else None
    )
    projected: list[ModelInputItem] = []
    for message in messages:
        projected.append(
            _conversation_message(
                message,
                active_trajectory=(
                    isinstance(message, AssistantMessage)
                    and message.uuid == active_trajectory_uuid
                ),
                replay_continuation=replay_continuation,
                active_binding=active_binding,
            )
        )
        projected.extend(
            attachment_projector.project_many(tuple(by_anchor.get(message.uuid, ())))
        )
    return projected


def _selected_continuation(
    continuation: ProviderContinuationState | None,
    active_trajectory: bool,
    replay: bool,
    active_binding: ProviderBinding | None = None,
) -> ProviderContinuationState | None:
    if continuation is None or not replay:
        return None
    if active_binding is not None and continuation.binding != active_binding:
        return None
    if continuation.replay_scope == "working_context":
        return continuation
    return continuation if active_trajectory else None
