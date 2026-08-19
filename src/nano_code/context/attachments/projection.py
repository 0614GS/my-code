"""ContextAttachment 到 provider-neutral 模型消息的唯一投影边界。"""

from nano_code.context.attachments.models import (
    ContextAttachment,
    ContextObservation,
)
from nano_code.context.documents import ContextInstruction
from nano_code.context.xml import render_context_instruction, wrap_xml
from nano_code.conversation import TextContent
from nano_code.model import (
    ModelTextBlock,
    ModelUserMessage,
)


class AttachmentProjector:
    """把可信 attachment payload 投影为合法的模型角色序列。"""

    def project(self, attachment: ContextAttachment) -> ModelUserMessage:
        content: list[ModelTextBlock] = []
        for block in attachment.content:
            if isinstance(block, TextContent):
                content.append(ModelTextBlock(block.text))
            elif isinstance(block, ContextInstruction):
                content.append(ModelTextBlock(render_context_instruction(block)))
            elif isinstance(block, ContextObservation):
                content.append(
                    ModelTextBlock(
                        wrap_xml(
                            "system-reminder",
                            "The user explicitly attached the following context: "
                            f"{block.title}\n\n{block.body}",
                        )
                    )
                )
        return ModelUserMessage(tuple(content))

    def project_many(
        self, attachments: tuple[ContextAttachment, ...]
    ) -> tuple[ModelUserMessage, ...]:
        return tuple(self.project(attachment) for attachment in attachments)

    def measure(self, attachments: tuple[ContextAttachment, ...]) -> int:
        return sum(
            sum(
                len(block.text)
                for block in message.content
                if isinstance(block, ModelTextBlock)
            )
            for message in self.project_many(attachments)
        )


__all__ = ["AttachmentProjector"]
