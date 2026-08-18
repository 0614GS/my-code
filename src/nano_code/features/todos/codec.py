"""TodoWrite JSON input conversion at the Tool/feature boundary."""

from typing import cast

from nano_code.conversation import JsonObject
from nano_code.features.todos.models import TODO_STATUSES, TodoItem, TodoStatus

TODO_WRITE_TOOL_NAME = "TodoWrite"


def parse_todo_input(tool_input: JsonObject) -> tuple[TodoItem, ...]:
    """Strictly parse a complete TodoWrite input object."""

    if set(tool_input) != {"todos"}:
        raise ValueError("input must contain only 'todos'")
    raw_todos = tool_input.get("todos")
    if not isinstance(raw_todos, list):
        raise ValueError("'todos' must be an array")

    todos: list[TodoItem] = []
    for index, raw_item in enumerate(raw_todos):
        if not isinstance(raw_item, dict):
            raise ValueError(f"todos[{index}] must be an object")
        if set(raw_item) != {"content", "status", "activeForm"}:
            raise ValueError(
                f"todos[{index}] must contain content, status, and activeForm only"
            )
        content = raw_item.get("content")
        status = raw_item.get("status")
        active_form = raw_item.get("activeForm")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"todos[{index}].content must be a non-empty string")
        if not isinstance(status, str) or status not in TODO_STATUSES:
            raise ValueError(
                f"todos[{index}].status must be pending, in_progress, or completed"
            )
        if not isinstance(active_form, str) or not active_form.strip():
            raise ValueError(f"todos[{index}].activeForm must be a non-empty string")
        todos.append(TodoItem(content, cast(TodoStatus, status), active_form))
    return tuple(todos)


__all__ = ["TODO_WRITE_TOOL_NAME", "parse_todo_input"]
