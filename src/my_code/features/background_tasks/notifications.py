"""Single-delivery background task completion attachments."""

from my_code.context.session_cache import AttachmentProjectionInput
from my_code.conversation.attachments import (
    AttachmentPayload,
    BackgroundTaskCompletionAttachment,
)
from my_code.conversation.models import AttachmentMessage
from my_code.features.background_tasks.registry import BackgroundTaskRegistry


class BackgroundTaskNotificationSource:
    """Expose terminal child tasks at model-request boundaries exactly once."""

    def __init__(self, registry: BackgroundTaskRegistry) -> None:
        self.registry = registry

    def __call__(
        self, state: AttachmentProjectionInput
    ) -> tuple[BackgroundTaskCompletionAttachment, ...]:
        owner_run_id = state.session_id
        already_in_session = {
            message.payload.task_id
            for message in state.conversation
            if isinstance(message, AttachmentMessage)
            and isinstance(message.payload, BackgroundTaskCompletionAttachment)
            and message.payload.owner_run_id == owner_run_id
        }
        return tuple(
            BackgroundTaskCompletionAttachment(
                owner_run_id,
                item.task_id,
                self.registry.payload(item),
            )
            for item in self.registry.pending(owner_run_id)
            if item.task_id not in already_in_session
        )

    def has_pending(self, owner_run_id: str) -> bool:
        """Return whether the owner has an undelivered terminal completion."""

        return bool(self.registry.pending(owner_run_id))

    def acknowledge(self, attachments: tuple[AttachmentPayload, ...]) -> None:
        grouped: dict[str, list[str]] = {}
        for attachment in attachments:
            if not isinstance(attachment, BackgroundTaskCompletionAttachment):
                continue
            grouped.setdefault(attachment.owner_run_id, []).append(attachment.task_id)
        for owner_run_id, task_ids in grouped.items():
            self.registry.acknowledge(owner_run_id, tuple(task_ids))


__all__ = ["BackgroundTaskNotificationSource"]
