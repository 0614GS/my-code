"""ContextAttachment 到 provider-neutral 模型消息的唯一投影边界。"""

from nano_code.agent.contracts.model import (
    ModelAssistantMessage,
    ModelMessage,
    ModelTextBlock,
    ModelToolResultBlock,
    ModelToolUseBlock,
    ModelUserMessage,
)
from nano_code.context.attachments.models import (
    AttachmentToolExchange,
    ContextAttachment,
)
from nano_code.context.documents import ContextInstruction
from nano_code.context.xml import render_context_instruction
from nano_code.conversation import TextContent


class AttachmentProjector:
    """把可信 attachment payload 投影为合法的模型角色序列。"""

    def project(self, attachment: ContextAttachment) -> tuple[ModelMessage, ...]:
        messages: list[ModelMessage] = []
        for block in attachment.content:
            if isinstance(block, TextContent):
                messages.append(ModelUserMessage((ModelTextBlock(block.text),)))
            elif isinstance(block, ContextInstruction):
                messages.append(
                    ModelUserMessage(
                        (ModelTextBlock(render_context_instruction(block)),)
                    )
                )
            elif isinstance(block, AttachmentToolExchange):
                messages.extend(
                    (
                        ModelAssistantMessage(
                            (
                                ModelToolUseBlock(
                                    block.tool_use_id,
                                    block.tool_name,
                                    block.tool_input,
                                ),
                            )
                        ),
                        ModelUserMessage(
                            (
                                ModelToolResultBlock(
                                    block.tool_use_id,
                                    block.result_content,
                                    block.is_error,
                                ),
                            )
                        ),
                    )
                )
        return tuple(messages)

    def project_many(
        self, attachments: tuple[ContextAttachment, ...]
    ) -> tuple[ModelMessage, ...]:
        return tuple(
            message
            for attachment in attachments
            for message in self.project(attachment)
        )

    def measure(self, attachments: tuple[ContextAttachment, ...]) -> int:
        return sum(
            _message_chars(message) for message in self.project_many(attachments)
        )


def _message_chars(message: ModelMessage) -> int:
    size = 0
    for block in message.content:
        if isinstance(block, ModelTextBlock):
            size += len(block.text)
        elif isinstance(block, ModelToolUseBlock):
            size += len(block.name) + len(str(block.input))
        else:
            size += len(block.content)
    return size


__all__ = ["AttachmentProjector"]
