"""TodoWrite 输入的强类型领域模型。"""

from dataclasses import dataclass
from typing import Literal, cast

from nano_code.messages import JsonObject

type TodoStatus = Literal["pending", "in_progress", "completed"]

_TODO_STATUSES = frozenset(("pending", "in_progress", "completed"))


@dataclass(frozen=True, slots=True)
class TodoItem:
    """一个模型管理的任务及其进行时展示文本。"""

    content: str
    status: TodoStatus
    active_form: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Todo content must not be empty")
        if self.status not in _TODO_STATUSES:
            raise ValueError(f"Unsupported todo status: {self.status}")
        if not self.active_form.strip():
            raise ValueError("Todo activeForm must not be empty")


def parse_todo_input(tool_input: JsonObject) -> tuple[TodoItem, ...]:
    """严格解析 TodoWrite 的完整输入对象。"""

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
        if not isinstance(status, str) or status not in _TODO_STATUSES:
            raise ValueError(
                f"todos[{index}].status must be pending, in_progress, or completed"
            )
        if not isinstance(active_form, str) or not active_form.strip():
            raise ValueError(f"todos[{index}].activeForm must be a non-empty string")
        todos.append(TodoItem(content, cast(TodoStatus, status), active_form))
    return tuple(todos)


__all__ = ["TodoItem", "TodoStatus", "parse_todo_input"]
