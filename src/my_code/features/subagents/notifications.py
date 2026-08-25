"""Single-delivery background task completion attachments."""

from my_code.context.session import ContextSnapshot
from my_code.conversation.attachments import (
    AttachmentPayload,
    BackgroundTaskCompletionAttachment,
)
from my_code.conversation.models import AttachmentMessage
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.serialization import background_subagent_payload


class BackgroundTaskNotificationSource:
    """Expose terminal child tasks at model-request boundaries exactly once."""

    def __init__(self, controller: SubagentController) -> None:
        self.controller = controller

    def __call__(
        self, snapshot: ContextSnapshot
    ) -> tuple[BackgroundTaskCompletionAttachment, ...]:
        owner_run_id = snapshot.session_id
        if owner_run_id is None:
            return ()
        already_in_session = {
            message.payload.task_id
            for message in (snapshot.session_history or snapshot.messages)
            if isinstance(message, AttachmentMessage)
            and isinstance(message.payload, BackgroundTaskCompletionAttachment)
            and message.payload.owner_run_id == owner_run_id
        }
        return tuple(
            BackgroundTaskCompletionAttachment(
                owner_run_id,
                item.task.task_id,
                background_subagent_payload(item),
            )
            for item in self.controller.pending_notifications(owner_run_id)
            if item.task.task_id not in already_in_session
        )

    def acknowledge(self, attachments: tuple[AttachmentPayload, ...]) -> None:
        grouped: dict[str, list[str]] = {}
        for attachment in attachments:
            if not isinstance(attachment, BackgroundTaskCompletionAttachment):
                continue
            grouped.setdefault(attachment.owner_run_id, []).append(attachment.task_id)
        for owner_run_id, task_ids in grouped.items():
            self.controller.acknowledge_notifications(
                owner_run_id,
                tuple(task_ids),
            )


__all__ = ["BackgroundTaskNotificationSource"]
