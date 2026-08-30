"""Project current Todo state from append-only conversation facts."""

from dataclasses import dataclass

from my_code.conversation.models import (
    AssistantMessage,
    ConversationEntry,
    ToolCall,
    ToolResultBatch,
)
from my_code.features.todos.codec import TODO_WRITE_TOOL_NAME, parse_todo_input
from my_code.features.todos.models import TodoItem
from my_code.tools.discovery import unwrap_searched_tool_call


@dataclass(frozen=True, slots=True)
class TodoProjection:
    """Latest TodoWrite state and completed model calls since that write."""

    todos: tuple[TodoItem, ...]
    completed_model_calls_since_write: int
    latest_write_id: str | None = None
    latest_write_todos: tuple[TodoItem, ...] | None = None


def project_todos(messages: tuple[ConversationEntry, ...]) -> TodoProjection:
    """Project the latest successful TodoWrite from active session history."""

    completed_model_calls = _completed_model_calls_since_write(messages)
    successful_ids = {
        result.tool_use_id
        for message in messages
        if isinstance(message, ToolResultBatch)
        for result in message.content
        if not result.is_error
    }
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        for block in reversed(message.content):
            call = (
                unwrap_searched_tool_call(block)
                if isinstance(block, ToolCall)
                else None
            )
            if (
                call is None
                or call.name != TODO_WRITE_TOOL_NAME
                or call.id not in successful_ids
            ):
                continue
            try:
                written_todos = parse_todo_input(call.input)
            except (TypeError, ValueError):
                continue
            active_todos = written_todos
            if written_todos and all(
                todo.status == "completed" for todo in written_todos
            ):
                active_todos = ()
            return TodoProjection(
                active_todos,
                completed_model_calls,
                call.id,
                written_todos,
            )
    return TodoProjection((), completed_model_calls)


def _completed_model_calls_since_write(
    messages: tuple[ConversationEntry, ...],
) -> int:
    completed_calls = 0
    for message in reversed(messages):
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


__all__ = [
    "TodoProjection",
    "project_todos",
]
