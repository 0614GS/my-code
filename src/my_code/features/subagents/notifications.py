"""Single-delivery background task completion attachments."""

import json

from my_code.context.attachments.models import (
    ContextAttachment,
    ContextObservation,
)
from my_code.context.session import AttachmentDelivery, ContextSnapshot
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.serialization import background_subagent_payload

_SOURCE_PREFIX = "background_task/"


class BackgroundTaskNotificationSource:
    """Expose terminal child tasks at model-request boundaries exactly once."""

    def __init__(self, controller: SubagentController) -> None:
        self.controller = controller

    def __call__(self, snapshot: ContextSnapshot) -> tuple[ContextAttachment, ...]:
        owner_run_id = snapshot.session_id
        if owner_run_id is None:
            return ()
        already_in_session = set(snapshot.delivered_attachment_sources)
        return tuple(
            ContextAttachment(
                source=_source(owner_run_id, item.task.task_id),
                content=(
                    ContextObservation(
                        "Background task completed",
                        json.dumps(
                            background_subagent_payload(item),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                ),
                retention="live_session",
            )
            for item in self.controller.pending_notifications(owner_run_id)
            if _source(owner_run_id, item.task.task_id) not in already_in_session
        )

    def acknowledge(self, deliveries: tuple[AttachmentDelivery, ...]) -> None:
        grouped: dict[str, list[str]] = {}
        for delivery in deliveries:
            identity = _parse_source(delivery.attachment.source)
            if identity is None:
                continue
            owner_run_id, task_id = identity
            grouped.setdefault(owner_run_id, []).append(task_id)
        for owner_run_id, task_ids in grouped.items():
            self.controller.acknowledge_notifications(
                owner_run_id,
                tuple(task_ids),
            )


def _source(owner_run_id: str, task_id: str) -> str:
    return f"{_SOURCE_PREFIX}{owner_run_id}/{task_id}"


def _parse_source(source: str) -> tuple[str, str] | None:
    if not source.startswith(_SOURCE_PREFIX):
        return None
    values = source.removeprefix(_SOURCE_PREFIX).split("/", 1)
    if len(values) != 2 or not all(value.strip() for value in values):
        return None
    return values[0], values[1]


__all__ = ["BackgroundTaskNotificationSource"]
