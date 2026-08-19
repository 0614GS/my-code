"""Non-persistent context state scoped to one active session."""

from dataclasses import dataclass, field
from uuid import uuid4

from nano_code.context.attachments.models import ContextAttachment
from nano_code.conversation import (
    ContentReplacement,
    ConversationMessage,
    ConversationSnapshot,
)


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
    messages: tuple[ConversationMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = ()
    session_history: tuple[ConversationMessage, ...] = ()
    attachment_deliveries: tuple[AttachmentDelivery, ...] = ()


class ContextSession:
    """Live-session delivery state; rebuild this object on resume/switch."""

    def __init__(self) -> None:
        self._deliveries: tuple[AttachmentDelivery, ...] = ()

    def snapshot(self, conversation: ConversationSnapshot) -> ContextSnapshot:
        working_ids = {message.uuid for message in conversation.messages}
        deliveries = tuple(
            item for item in self._deliveries if item.anchor_uuid in working_ids
        )
        return ContextSnapshot(
            conversation.messages,
            conversation.content_replacements,
            conversation.session_history,
            deliveries,
        )

    def add(
        self,
        deliveries: tuple[AttachmentDelivery, ...],
        conversation: ConversationSnapshot,
    ) -> None:
        working_ids = {message.uuid for message in conversation.messages}
        existing = {item.delivery_id: item for item in self._deliveries}
        pending: list[AttachmentDelivery] = []
        for delivery in deliveries:
            if delivery.anchor_uuid not in working_ids:
                raise ValueError(
                    "Attachment delivery anchor is not in the working set: "
                    f"{delivery.anchor_uuid}"
                )
            previous = existing.get(delivery.delivery_id)
            if previous is not None:
                if previous != delivery:
                    raise ValueError(
                        f"Conflicting attachment delivery: {delivery.delivery_id}"
                    )
                continue
            existing[delivery.delivery_id] = delivery
            pending.append(delivery)
        self._deliveries += tuple(pending)


__all__ = ["AttachmentDelivery", "ContextSession", "ContextSnapshot"]
