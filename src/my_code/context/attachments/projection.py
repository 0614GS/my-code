"""ContextAttachment 到 provider-neutral 用户输入的唯一投影边界。"""

from my_code.context.attachments.models import (
    ContextAttachment,
    ContextObservation,
)
from my_code.context.documents import ContextInstruction
from my_code.context.xml import render_context_instruction, wrap_xml
from my_code.conversation.models import TextContent
from my_code.model.request import InputText, UserInput


class AttachmentProjector:
    """把可信 attachment payload 投影为合法的模型角色序列。"""

    def project(self, attachment: ContextAttachment) -> UserInput:
        content: list[InputText] = []
        for block in attachment.content:
            if isinstance(block, TextContent):
                content.append(InputText(block.text))
            elif isinstance(block, ContextInstruction):
                content.append(InputText(render_context_instruction(block)))
            elif isinstance(block, ContextObservation):
                content.append(
                    InputText(
                        wrap_xml(
                            "system-reminder",
                            "The user explicitly attached the following context: "
                            f"{block.title}\n\n{block.body}",
                        )
                    )
                )
        return UserInput(tuple(content))

    def project_many(
        self, attachments: tuple[ContextAttachment, ...]
    ) -> tuple[UserInput, ...]:
        return tuple(self.project(attachment) for attachment in attachments)

    def measure(self, attachments: tuple[ContextAttachment, ...]) -> int:
        return sum(
            sum(
                len(block.text)
                for block in message.content
                if isinstance(block, InputText)
            )
            for message in self.project_many(attachments)
        )


__all__ = ["AttachmentProjector"]
