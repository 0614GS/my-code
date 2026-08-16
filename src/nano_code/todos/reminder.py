"""TodoWrite 的非持久化 session-runtime reminder attachment。"""

from nano_code.agent.contracts.session import ConversationSnapshot
from nano_code.messages import (
    AssistantMessage,
    ContextAttachment,
    ContextInstruction,
    ToolCall,
)
from nano_code.todos.projection import TODO_WRITE_TOOL_NAME, project_todos

TODO_REMINDER_TURNS = 10

_REMINDER = (
    "The TodoWrite tool hasn't been used recently. If you're working on tasks "
    "that would benefit from tracking progress, consider using the TodoWrite "
    "tool to track progress. Also consider cleaning up the todo list if it has "
    "become stale and no longer matches what you are working on. Only use it if "
    "it's relevant to the current work. This is just a gentle reminder - ignore "
    "if not applicable. Make sure that you NEVER mention this reminder to the user"
)


class TodoReminderSource:
    """每隔十个未使用 TodoWrite 的 assistant turns 注入一次提醒。"""

    def __call__(self, snapshot: ConversationSnapshot) -> tuple[ContextAttachment, ...]:
        projection = project_todos(snapshot.session_history or snapshot.messages)
        turns_since_write = _assistant_turns_since_todo_write(snapshot)
        turns_since_reminder = _assistant_turns_since_reminder(snapshot)
        if (
            turns_since_write < TODO_REMINDER_TURNS
            or turns_since_reminder < TODO_REMINDER_TURNS
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
                lifecycle="session_runtime",
            ),
        )


def _assistant_turns_since_todo_write(snapshot: ConversationSnapshot) -> int:
    turns = 0
    for message in reversed(snapshot.messages):
        if not isinstance(message, AssistantMessage):
            continue
        if any(
            isinstance(block, ToolCall) and block.name == TODO_WRITE_TOOL_NAME
            for block in message.content
        ):
            return turns
        turns += 1
    return turns


def _assistant_turns_since_reminder(snapshot: ConversationSnapshot) -> int:
    anchors = {
        delivery.after_message_uuid
        for delivery in snapshot.runtime_attachments
        if delivery.attachment.source == "todo_reminder"
    }
    turns = 0
    for message in reversed(snapshot.messages):
        if message.uuid in anchors:
            return turns
        if isinstance(message, AssistantMessage):
            turns += 1
    return turns


__all__ = ["TODO_REMINDER_TURNS", "TodoReminderSource"]
