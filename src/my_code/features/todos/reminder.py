"""Non-persistent, live-session TodoWrite reminder attachment."""

from my_code.context.session import ContextSnapshot
from my_code.conversation.attachments import TodoReminderAttachment
from my_code.conversation.models import AssistantMessage, AttachmentMessage, ToolCall
from my_code.features.todos.codec import TODO_WRITE_TOOL_NAME
from my_code.features.todos.projection import project_todos

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

    def __call__(self, snapshot: ContextSnapshot) -> tuple[TodoReminderAttachment, ...]:
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
        return (TodoReminderAttachment(content),)


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
    completed_calls = 0
    for message in reversed(snapshot.messages):
        if isinstance(message, AttachmentMessage) and isinstance(
            message.payload, TodoReminderAttachment
        ):
            return completed_calls
        if isinstance(message, AssistantMessage):
            completed_calls += 1
    return completed_calls


__all__ = [
    "TODO_REMINDER_MODEL_CALL_INTERVAL",
    "TodoReminderAttachmentSource",
]
