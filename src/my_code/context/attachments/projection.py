"""Conversation attachment payload 到标准用户输入的唯一投影边界。"""

import json

from my_code.context.xml import wrap_xml
from my_code.conversation.attachments import (
    AttachmentPayload,
    BackgroundTaskCompletionAttachment,
    FileMentionAttachment,
    SkillActivationAttachment,
    SkillListingAttachment,
    TodoReminderAttachment,
)
from my_code.model.request import InputText, UserInput


class AttachmentProjector:
    """把可信 attachment payload 投影为合法的模型角色序列。"""

    def project(self, attachment: AttachmentPayload) -> UserInput:
        return UserInput((InputText(_render(attachment)),))

    def project_many(
        self, attachments: tuple[AttachmentPayload, ...]
    ) -> tuple[UserInput, ...]:
        return tuple(self.project(attachment) for attachment in attachments)

    def measure(self, attachments: tuple[AttachmentPayload, ...]) -> int:
        return sum(
            sum(
                len(block.text)
                for block in message.content
                if isinstance(block, InputText)
            )
            for message in self.project_many(attachments)
        )


def _render(attachment: AttachmentPayload) -> str:
    if isinstance(attachment, FileMentionAttachment):
        title = (
            ("Directory" if attachment.is_directory else "File")
            + ": "
            + attachment.path
        )
        return wrap_xml(
            "system-reminder",
            "The user explicitly attached the following context: "
            f"{title}\n\n{attachment.body}",
        )
    if isinstance(attachment, TodoReminderAttachment):
        return wrap_xml("system-reminder", attachment.content)
    if isinstance(attachment, BackgroundTaskCompletionAttachment):
        return wrap_xml(
            "system-reminder",
            "Background task completed\n\n"
            + json.dumps(attachment.result, ensure_ascii=False, sort_keys=True),
        )
    if isinstance(attachment, SkillListingAttachment):
        lines = "\n".join(
            f"- {skill.name}: {skill.description} (source: {skill.source})"
            for skill in attachment.skills
        )
        return wrap_xml(
            "system-reminder",
            "The following skills are available for use with the Skill tool:\n\n"
            + lines,
        )
    if isinstance(attachment, SkillActivationAttachment):
        return _render_skill(attachment)
    return wrap_xml(
        "system-reminder",
        "The following skills were invoked in this session. Continue to follow "
        "these guidelines:\n\n"
        + "\n\n".join(_render_skill(skill) for skill in attachment.skills),
    )


def _render_skill(skill: SkillActivationAttachment) -> str:
    compatibility = (
        f"\nCompatibility: {skill.compatibility}" if skill.compatibility else ""
    )
    return (
        f'<skill name="{skill.name}">\n'
        f"Source: {skill.source}\nLocator: {skill.locator}{compatibility}\n\n"
        f"{skill.instructions}\n</skill>"
    )


__all__ = ["AttachmentProjector"]
