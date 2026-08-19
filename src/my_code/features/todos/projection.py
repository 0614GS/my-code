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


@dataclass(frozen=True, slots=True)
class TodoProjection:
    """Latest TodoWrite state and completed model calls since that write."""

    todos: tuple[TodoItem, ...]
    completed_model_calls_since_write: int


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
            if (
                not isinstance(block, ToolCall)
                or block.name != TODO_WRITE_TOOL_NAME
                or block.id not in successful_ids
            ):
                continue
            try:
                todos = parse_todo_input(block.input)
            except (TypeError, ValueError):
                continue
            if todos and all(todo.status == "completed" for todo in todos):
                todos = ()
            return TodoProjection(todos, completed_model_calls)
    return TodoProjection((), completed_model_calls)


def _completed_model_calls_since_write(
    messages: tuple[ConversationEntry, ...],
) -> int:
    completed_calls = 0
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        if any(
            isinstance(block, ToolCall) and block.name == TODO_WRITE_TOOL_NAME
            for block in message.content
        ):
            return completed_calls
        completed_calls += 1
    return completed_calls


__all__ = [
    "TodoProjection",
    "project_todos",
]
