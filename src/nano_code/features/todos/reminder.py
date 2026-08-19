"""Non-persistent, live-session TodoWrite reminder attachment."""

from nano_code.context import ContextSnapshot
from nano_code.context.attachments.models import ContextAttachment
from nano_code.context.documents import ContextInstruction
from nano_code.conversation import AssistantMessage, ToolCall
from nano_code.features.todos.codec import TODO_WRITE_TOOL_NAME
from nano_code.features.todos.projection import project_todos

TODO_REMINDER_MODEL_CALL_INTERVAL = 10

_REMINDER = (
    "The TodoWrite tool hasn't been used recently. If you're working on tasks "
    "that would benefit from tracking progress, consider using the TodoWrite "
    "tool to track progress. Also consider cleaning up the todo list if it has "
    "become stale and no longer matches what you are working on. Only use it if "
    "it's relevant to the current work. This is just a gentle reminder - ignore "
    "if not applicable. Make sure that you NEVER mention this reminder to the user"
)


class TodoReminderAttachmentSource:
    """Inject a reminder every ten completed model calls without TodoWrite."""

    def __call__(self, snapshot: ContextSnapshot) -> tuple[ContextAttachment, ...]:
        projection = project_todos(snapshot.session_history or snapshot.messages)
        calls_since_write = _completed_model_calls_since_todo_write(snapshot)
        calls_since_reminder = _completed_model_calls_since_reminder(snapshot)
        if (
            calls_since_write < TODO_REMINDER_MODEL_CALL_INTERVAL
            or calls_since_reminder < TODO_REMINDER_MODEL_CALL_INTERVAL
        ):
            return ()

        content = _REMINDER
        if projection.todos:
            items = "\n".join(
                f"{index}. [{todo.status}] {todo.content}"
                for index, todo in enumerate(projection.todos, start=1)
            )
            content += (
                f"\n\nHere are the existing contents of your todo list:\n\n[{items}]"
            )
        return (
            ContextAttachment(
                source="todo_reminder",
                content=(ContextInstruction(content),),
                retention="live_session",
            ),
        )


def _completed_model_calls_since_todo_write(snapshot: ContextSnapshot) -> int:
    completed_calls = 0
    for message in reversed(snapshot.messages):
        if not isinstance(message, AssistantMessage):
            continue
        if any(
            isinstance(block, ToolCall) and block.name == TODO_WRITE_TOOL_NAME
            for block in message.content
        ):
            return completed_calls
        completed_calls += 1
    return completed_calls


def _completed_model_calls_since_reminder(snapshot: ContextSnapshot) -> int:
    anchors = {
        delivery.anchor_uuid
        for delivery in snapshot.attachment_deliveries
        if delivery.attachment.source == "todo_reminder"
    }
    completed_calls = 0
    for message in reversed(snapshot.messages):
        if message.uuid in anchors:
            return completed_calls
        if isinstance(message, AssistantMessage):
            completed_calls += 1
    return completed_calls


__all__ = ["TODO_REMINDER_MODEL_CALL_INTERVAL", "TodoReminderAttachmentSource"]
