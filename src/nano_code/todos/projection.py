"""从追加式会话事实投影当前 Todo 状态。"""

from dataclasses import dataclass

from nano_code.messages import (
    AssistantMessage,
    ConversationMessage,
    ToolCall,
    ToolResultsMessage,
)
from nano_code.todos.models import TodoItem, parse_todo_input

TODO_WRITE_TOOL_NAME = "TodoWrite"


@dataclass(frozen=True, slots=True)
class TodoProjection:
    """最新 TodoWrite 状态及距其经过的 completed model call 数。"""

    todos: tuple[TodoItem, ...]
    completed_model_calls_since_write: int


def project_todos(messages: tuple[ConversationMessage, ...]) -> TodoProjection:
    """按活动 session 历史中的最后一次成功 TodoWrite 投影状态。"""

    completed_model_calls = _completed_model_calls_since_write(messages)
    successful_ids = {
        result.tool_use_id
        for message in messages
        if isinstance(message, ToolResultsMessage)
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
            # 与 CC 的运行时 AppState 一致：最后一项完成后清空可见列表，
            # Transcript 中仍保留原始 tool input 作为恢复事实。
            if todos and all(todo.status == "completed" for todo in todos):
                todos = ()
            return TodoProjection(todos, completed_model_calls)
    return TodoProjection((), completed_model_calls)


def _completed_model_calls_since_write(
    messages: tuple[ConversationMessage, ...],
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


__all__ = ["TODO_WRITE_TOOL_NAME", "TodoProjection", "project_todos"]
