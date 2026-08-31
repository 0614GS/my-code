"""ConversationEntry 到 ModelInputItem 的纯投影与协议校验。"""

from my_code.context.attachments.projection import AttachmentProjector
from my_code.context.documents import UserContextDocument
from my_code.context.xml import render_context_instruction, wrap_xml
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    ConversationEntry,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultBatch,
)
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuation,
    ProviderReplayRecord,
    replay_content_id,
)
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
        replay_records: tuple[ProviderReplayRecord, ...] = (),
        active_binding: ProviderBinding | None = None,
    ) -> tuple[ModelInputItem, ...]:
        candidates = [
            *(_context_message(item) for item in user_context),
            *_conversation_history(
                history,
                self.attachment_projector,
                replay_continuation=True,
                replay_records=replay_records,
                active_binding=active_binding,
            ),
        ]
        result = tuple(candidates)
        validate_model_input(result)
        return result

    def normalize_transcript(
        self,
        messages: tuple[ConversationEntry, ...],
    ) -> tuple[ModelInputItem, ...]:
        """Compact 使用的 history-only 视图。"""

        result = tuple(
            _conversation_history(
                messages,
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
    replay_records: dict[str, ProviderContinuation] | None = None,
    active_binding: ProviderBinding | None = None,
) -> ModelInputItem:
    if isinstance(message, AttachmentMessage):
        raise TypeError("Attachment messages require AttachmentProjector")
    if isinstance(message, HumanMessage):
        return UserInput((InputText(message.content),))
    if isinstance(message, AssistantMessage):
        replay_by_content = replay_records or {}
        return AssistantOutput(
            tuple(
                ModelTextBlock(
                    block.text,
                    _selected_continuation(
                        replay_by_content.get(replay_content_id(index)),
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
                        replay_by_content.get(replay_content_id(index)),
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
                        replay_by_content.get(replay_content_id(index)),
                        active_trajectory,
                        replay_continuation,
                        active_binding,
                    ),
                )
                for index, block in enumerate(message.content)
                if not isinstance(block, ReasoningContent)
                or _selected_continuation(
                    replay_by_content.get(replay_content_id(index)),
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
    attachment_projector: AttachmentProjector,
    *,
    replay_continuation: bool,
    replay_records: tuple[ProviderReplayRecord, ...] = (),
    active_binding: ProviderBinding | None = None,
) -> list[ModelInputItem]:
    last_protocol_entry = next(
        (
            item
            for item in reversed(messages)
            if not isinstance(item, AttachmentMessage)
        ),
        None,
    )
    active_trajectory_uuid = (
        last_protocol_entry.source_assistant_id
        if replay_continuation and isinstance(last_protocol_entry, ToolResultBatch)
        else None
    )
    projected: list[ModelInputItem] = []
    replay_by_entry: dict[str, dict[str, ProviderContinuation]] = {}
    for record in replay_records:
        replay_by_entry.setdefault(record.entry_id, {})[record.content_id] = (
            record.state
        )
    for message in messages:
        if isinstance(message, AttachmentMessage):
            projected.append(attachment_projector.project(message.payload))
            continue
        projected.append(
            _conversation_message(
                message,
                active_trajectory=(
                    isinstance(message, AssistantMessage)
                    and message.uuid == active_trajectory_uuid
                ),
                replay_continuation=replay_continuation,
                replay_records=replay_by_entry.get(message.uuid),
                active_binding=active_binding,
            )
        )
    return projected


def _selected_continuation(
    continuation: ProviderContinuation | None,
    active_trajectory: bool,
    replay: bool,
    active_binding: ProviderBinding | None = None,
) -> ProviderContinuation | None:
    if continuation is None or not replay:
        return None
    if active_binding is not None and continuation.binding != active_binding:
        return None
    if continuation.replay_scope == "working_context":
        return continuation
    return continuation if active_trajectory else None
