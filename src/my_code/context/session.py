"""Immutable context views and the narrow Session context protocol."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from my_code.context.attachments.models import ContextAttachment
from my_code.context.documents import UserContextDocument
from my_code.conversation.models import ConversationEntry
from my_code.conversation.state import ContentReplacement
from my_code.model.primitives import ProviderReplayRecord
from my_code.model.request import SystemPrompt
from my_code.prompts.registry import PromptRegistry


@dataclass(frozen=True, slots=True)
class AttachmentDelivery:
    anchor_uuid: str
    attachment: ContextAttachment
    delivery_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not self.anchor_uuid or not self.delivery_id:
            raise ValueError("Attachment delivery ID and anchor must not be empty")
        if self.attachment.retention != "live_session":
            raise ValueError("Delivered attachments must use live_session retention")


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    messages: tuple[ConversationEntry, ...]
    content_replacements: tuple[ContentReplacement, ...] = ()
    session_history: tuple[ConversationEntry, ...] = ()
    attachment_deliveries: tuple[AttachmentDelivery, ...] = ()
    replay_records: tuple[ProviderReplayRecord, ...] = ()
    session_id: str | None = None
    delivered_attachment_sources: tuple[str, ...] = ()


class SessionContextAccess(Protocol):
    """Operations backed by Session-owned, non-persistent context state."""

    def resolve_prompt(self, registry: PromptRegistry) -> SystemPrompt: ...

    def user_context(
        self,
        resolve: Callable[[], tuple[UserContextDocument, ...]],
    ) -> tuple[UserContextDocument, ...]: ...


__all__ = [
    "AttachmentDelivery",
    "ContextSnapshot",
    "SessionContextAccess",
]
