"""Non-persistent, live-session TodoWrite reminder attachment."""

from my_code.context.session import AttachmentDerivationState
from my_code.conversation.attachments import TodoReminderAttachment
from my_code.conversation.models import AssistantMessage, AttachmentMessage, ToolCall
from my_code.features.todos.codec import TODO_WRITE_TOOL_NAME
from my_code.features.todos.projection import project_todos
from my_code.model.tool_search import ToolSearchMode
from my_code.tools.discovery import restored_discoveries, unwrap_searched_tool_call

TODO_REMINDER_MODEL_CALL_INTERVAL = 10


class TodoReminderAttachmentSource:
    """Inject a reminder every ten completed model calls without TodoWrite."""

    def __init__(self, mode: ToolSearchMode = ToolSearchMode.DISPATCHER) -> None:
        self.mode = mode

    def __call__(
        self, state: AttachmentDerivationState
    ) -> tuple[TodoReminderAttachment, ...]:
        projection = project_todos(state.conversation)
        calls_since_write = _completed_model_calls_since_todo_write(state)
        calls_since_reminder = _completed_model_calls_since_reminder(state)
        if (
            calls_since_write < TODO_REMINDER_MODEL_CALL_INTERVAL
            or calls_since_reminder < TODO_REMINDER_MODEL_CALL_INTERVAL
        ):
            return ()

        discovered = TODO_WRITE_TOOL_NAME in restored_discoveries(state.conversation)
        content = _reminder(self.mode, discovered)
        if projection.todos:
            items = "\n".join(
                f"{index}. [{todo.status}] {todo.content}"
                for index, todo in enumerate(projection.todos, start=1)
            )
            content += (
                f"\n\nHere are the existing contents of your todo list:\n\n[{items}]"
            )
        return (TodoReminderAttachment(content),)


def _reminder(mode: ToolSearchMode, discovered: bool) -> str:
    if mode is ToolSearchMode.DISPATCHER:
        route = (
            'Update it only through InvokeSearchedTool with tool_name="TodoWrite". '
            if discovered
            else "Find TodoWrite with ToolSearch, then call it only through "
            "InvokeSearchedTool. "
        )
        prohibition = "Never call TodoWrite directly. "
    else:
        route = (
            "Update it with TodoWrite. "
            if discovered
            else "Find TodoWrite with ToolSearch, then call it next step. "
        )
        prohibition = ""
    return (
        "The todo list may be stale. If useful, "
        + route
        + prohibition
        + "Ignore otherwise. NEVER mention this reminder to the user."
    )


def _completed_model_calls_since_todo_write(state: AttachmentDerivationState) -> int:
    completed_calls = 0
    for message in reversed(state.context_entries):
        if not isinstance(message, AssistantMessage):
            continue
        if any(
            isinstance(block, ToolCall)
            and unwrap_searched_tool_call(block).name == TODO_WRITE_TOOL_NAME
            for block in message.content
        ):
            return completed_calls
        completed_calls += 1
    return completed_calls


def _completed_model_calls_since_reminder(state: AttachmentDerivationState) -> int:
    completed_calls = 0
    for message in reversed(state.context_entries):
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
