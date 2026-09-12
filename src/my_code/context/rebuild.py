"""无 I/O 的压缩后关键状态恢复，不消费普通通知或提交 Session。"""

from typing import Literal, Protocol

from my_code.context.session_cache import CompactionInput
from my_code.conversation.attachments import (
    AttachmentPayload,
    CollaborationModeAttachment,
    InvokedSkillsAttachment,
    SkillActivationAttachment,
    ToolDiscoveryAttachment,
    ToolDiscoveryDefinition,
    ToolDiscoveryInvalidationAttachment,
)
from my_code.conversation.models import AttachmentMessage, ConversationEntry


class PostCompactAttachmentSource(Protocol):
    """领域通过同步快照投影扩展恢复内容；失败必须使整个提案失败。"""

    def __call__(self, state: CompactionInput) -> tuple[AttachmentPayload, ...]: ...


class PostCompactContextRebuilder:
    """按模式、Skill、工具发现、领域附件顺序恢复当前有效状态。"""

    def __init__(self, sources: tuple[PostCompactAttachmentSource, ...] = ()) -> None:
        self._sources = sources

    def rebuild(self, state: CompactionInput) -> tuple[AttachmentPayload, ...]:
        mode = state.collaboration_mode
        if mode not in ("default", "plan"):
            raise ValueError("Unsupported collaboration mode")
        restored: list[AttachmentPayload] = [CollaborationModeAttachment(mode)]
        window = state.planning.context_entries
        invoked = _latest_invoked_skills(window)
        if invoked is not None:
            restored.append(invoked)
        restored.extend(_latest_tool_discoveries(window))
        for source in self._sources:
            restored.extend(source(state))
        return tuple(restored)


def _latest_invoked_skills(
    history: tuple[ConversationEntry, ...],
) -> InvokedSkillsAttachment | None:
    by_name: dict[str, SkillActivationAttachment] = {}
    for entry in history:
        if not isinstance(entry, AttachmentMessage):
            continue
        payload = entry.payload
        if isinstance(payload, SkillActivationAttachment):
            by_name[payload.name] = payload
        elif isinstance(payload, InvokedSkillsAttachment):
            for skill in payload.skills:
                by_name[skill.name] = skill
    if not by_name:
        return None
    return InvokedSkillsAttachment(tuple(by_name[name] for name in sorted(by_name)))


def _latest_tool_discoveries(
    history: tuple[ConversationEntry, ...],
) -> tuple[ToolDiscoveryAttachment, ...]:
    by_name: dict[
        str, tuple[ToolDiscoveryDefinition, Literal["dispatcher", "native"]]
    ] = {}
    for entry in history:
        if not isinstance(entry, AttachmentMessage):
            continue
        payload = entry.payload
        if isinstance(payload, ToolDiscoveryAttachment):
            by_name.update(
                (item.name, (item, payload.mode)) for item in payload.definitions
            )
        elif isinstance(payload, ToolDiscoveryInvalidationAttachment):
            for name in payload.names:
                by_name.pop(name, None)
    restored: list[ToolDiscoveryAttachment] = []
    for mode in ("dispatcher", "native"):
        definitions = tuple(
            by_name[name][0] for name in sorted(by_name) if by_name[name][1] == mode
        )
        if definitions:
            restored.append(ToolDiscoveryAttachment(definitions, mode))
    return tuple(restored)


__all__ = ["PostCompactAttachmentSource", "PostCompactContextRebuilder"]
