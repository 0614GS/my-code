from pathlib import Path

import pytest

from my_code.context.planner import ContextPlanner
from my_code.context.session import (
    AttachmentDerivationState,
    ContextPlanningState,
    ContextRuntime,
)
from my_code.conversation.attachments import ToolDiscoveryAttachment
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.todos.codec import parse_todo_input
from my_code.features.todos.projection import project_todos
from my_code.features.todos.reminder import TodoReminderAttachmentSource
from my_code.features.todos.tool import TodoWriteTool
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    AssistantOutput,
    InputText,
    ModelInputItem,
    ModelTextBlock,
    PromptStability,
    UserInput,
)
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import discovery_definition
from my_code.tools.executor import ToolExecutor
from my_code.workspace.local import Workspace


def _input_texts(items: tuple[ModelInputItem, ...]) -> list[str]:
    texts: list[str] = []
    for item in items:
        if isinstance(item, UserInput):
            texts.extend(
                block.text for block in item.content if isinstance(block, InputText)
            )
        elif isinstance(item, AssistantOutput):
            texts.extend(
                block.text
                for block in item.content
                if isinstance(block, ModelTextBlock)
            )
    return texts


def _todo_input(status: str = "in_progress") -> dict:
    return {
        "todos": [
            {
                "content": "Run tests",
                "status": status,
                "activeForm": "Running tests",
            }
        ]
    }


def _assistant(*content: TextContent | ToolCall) -> AssistantMessage:
    return AssistantMessage(content=content, usage=TokenUsage())


def _history_after_todo(
    completed_model_calls: int, *, status: str = "in_progress"
) -> tuple:
    assistant = _assistant(ToolCall("todo-1", "TodoWrite", _todo_input(status)))
    messages = [
        HumanMessage("work"),
        assistant,
        ToolResultBatch(
            content=(
                ToolResult("todo-1", "updated", ToolResultPresentation("updated")),
            ),
            source_assistant_id=assistant.uuid,
        ),
    ]
    messages.extend(
        _assistant(TextContent(f"call {index}"))
        for index in range(completed_model_calls)
    )
    return tuple(messages)


def test_todo_input_and_model_schema_are_strict() -> None:
    tool = TodoWriteTool()

    todos = parse_todo_input(_todo_input())

    assert todos[0].content == "Run tests"
    assert todos[0].active_form == "Running tests"
    assert tool.definition.name == "TodoWrite"
    assert tool.definition.input_schema["additionalProperties"] is False

    with pytest.raises(ValueError, match="only 'todos'"):
        parse_todo_input({**_todo_input(), "extra": True})
    with pytest.raises(ValueError, match="status"):
        parse_todo_input(_todo_input("blocked"))
    with pytest.raises(ValueError, match="activeForm"):
        parse_todo_input(
            {
                "todos": [
                    {
                        "content": "Run tests",
                        "status": "pending",
                        "activeForm": "",
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_todo_write_executes_without_permission_prompt(tmp_path: Path) -> None:
    tool = TodoWriteTool()
    executor = ToolExecutor(
        ToolCatalogSnapshot.from_tools((tool,)),
        PermissionPolicy(PermissionMode.DEFAULT),
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    outcome = await executor.execute(ToolCall("todo", "TodoWrite", _todo_input()))

    assert not outcome.result.is_error
    assert "modified successfully" in outcome.result.content
    assert outcome.result.presentation.summary == "Updated 1 todo(s)"


@pytest.mark.asyncio
async def test_invalid_todo_write_becomes_protocol_error(tmp_path: Path) -> None:
    executor = ToolExecutor(
        ToolCatalogSnapshot.from_tools((TodoWriteTool(),)),
        PermissionPolicy(PermissionMode.BYPASS),
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall("todo", "TodoWrite", {"todos": [{"content": "missing fields"}]})
    )

    assert outcome.result.is_error
    assert outcome.result.tool_use_id == "todo"
    assert "Invalid input" in outcome.result.content


def test_todo_projection_uses_latest_call_and_clears_all_completed() -> None:
    active = project_todos(_history_after_todo(3))
    completed = project_todos(_history_after_todo(3, status="completed"))

    assert [todo.content for todo in active.todos] == ["Run tests"]
    assert active.completed_model_calls_since_write == 3
    assert completed.todos == ()
    assert completed.completed_model_calls_since_write == 3
    assert completed.latest_write_todos is not None
    assert completed.latest_write_todos[0].status == "completed"


def test_todo_projection_recognizes_dispatcher_wrapped_writes() -> None:
    assistant = _assistant(
        ToolCall(
            "todo-dispatched",
            "InvokeSearchedTool",
            {"tool_name": "TodoWrite", "arguments": _todo_input("completed")},
        )
    )
    history = (
        HumanMessage("work"),
        assistant,
        ToolResultBatch(
            (ToolResult("todo-dispatched", "updated", ToolResultPresentation("ok")),),
            assistant.uuid,
        ),
    )

    projection = project_todos(history)

    assert projection.todos == ()
    assert projection.latest_write_id == "todo-dispatched"
    assert projection.latest_write_todos is not None
    assert projection.latest_write_todos[0].content == "Run tests"


def test_failed_todo_write_does_not_replace_last_successful_state() -> None:
    history = list(_history_after_todo(2))
    failed = _assistant(ToolCall("todo-failed", "TodoWrite", {"todos": "invalid"}))
    history.extend(
        (
            failed,
            ToolResultBatch(
                content=(
                    ToolResult(
                        "todo-failed",
                        "invalid",
                        ToolResultPresentation("invalid"),
                        is_error=True,
                    ),
                ),
                source_assistant_id=failed.uuid,
            ),
        )
    )

    projection = project_todos(tuple(history))

    assert [todo.content for todo in projection.todos] == ["Run tests"]
    assert projection.completed_model_calls_since_write == 0


def test_todo_reminder_uses_independent_write_and_delivery_thresholds() -> None:
    source = TodoReminderAttachmentSource()
    history10 = _history_after_todo(10)
    history9 = _history_after_todo(9)
    assert source(AttachmentDerivationState("session", history9, history9)) == ()

    attachments = source(AttachmentDerivationState("session", history10, history10))
    assert len(attachments) == 1
    assert "1. [in_progress] Run tests" in attachments[0].content
    assert "NEVER mention this reminder" in attachments[0].content

    reminder = AttachmentMessage(attachments[0], parent_uuid=history10[-1].uuid)
    history11 = history10 + (reminder, _assistant(TextContent("call 11")))
    assert source(AttachmentDerivationState("session", history11, history11)) == ()

    history20 = (
        history10
        + (reminder,)
        + tuple(_assistant(TextContent(f"later {index}")) for index in range(10))
    )
    assert len(source(AttachmentDerivationState("session", history20, history20))) == 1

    # reminder 不持久化：恢复时没有 delivery history，已经超过阈值会立即提醒，
    # 而不是等到 turns_since_write 恰好能被 10 整除。
    history15 = history10 + tuple(
        _assistant(TextContent(f"resumed {index}")) for index in range(5)
    )
    assert len(source(AttachmentDerivationState("session", history15, history15))) == 1


def test_todo_reminder_uses_full_session_history_after_compaction() -> None:
    history = _history_after_todo(10)
    compact_entries = tuple(
        _assistant(TextContent(f"post-compact {index}")) for index in range(10)
    )

    attachments = TodoReminderAttachmentSource()(
        AttachmentDerivationState("session", history, compact_entries)
    )

    assert len(attachments) == 1
    assert "Run tests" in attachments[0].content


def test_todo_reminder_without_prior_write_starts_after_ten_model_calls() -> None:
    history = tuple(_assistant(TextContent(str(index))) for index in range(10))

    attachments = TodoReminderAttachmentSource()(
        AttachmentDerivationState("session", history, history)
    )

    assert len(attachments) == 1
    assert "existing contents" not in attachments[0].content
    assert "Find TodoWrite with ToolSearch" in attachments[0].content
    assert "Never call TodoWrite directly" in attachments[0].content


def test_todo_reminder_uses_dispatcher_after_discovery() -> None:
    history = _history_after_todo(10)
    discovery = AttachmentMessage(
        ToolDiscoveryAttachment((discovery_definition(TodoWriteTool()),), "dispatcher"),
        parent_uuid=history[-1].uuid,
    )

    attachment = TodoReminderAttachmentSource(ToolSearchMode.DISPATCHER)(
        AttachmentDerivationState("session", history + (discovery,), history)
    )[0]

    assert 'InvokeSearchedTool with tool_name="TodoWrite"' in attachment.content
    assert "Find TodoWrite with ToolSearch" not in attachment.content
    assert "Never call TodoWrite directly" in attachment.content


def test_todo_reminder_preserves_native_route() -> None:
    history = _history_after_todo(10)
    discovery = AttachmentMessage(
        ToolDiscoveryAttachment((discovery_definition(TodoWriteTool()),), "native"),
        parent_uuid=history[-1].uuid,
    )

    attachment = TodoReminderAttachmentSource(ToolSearchMode.NATIVE)(
        AttachmentDerivationState("session", history + (discovery,), history)
    )[0]

    assert "Update it with TodoWrite" in attachment.content
    assert "InvokeSearchedTool" not in attachment.content


def test_context_planner_projects_reminder_from_conversation_for_compaction() -> None:
    history = _history_after_todo(10)
    tool = TodoWriteTool()
    planner = ContextPlanner(
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
    )
    attachment = TodoReminderAttachmentSource()(
        AttachmentDerivationState("session", history, history)
    )[0]
    reminder = AttachmentMessage(attachment, parent_uuid=history[-1].uuid)
    state = ContextPlanningState(history + (reminder,))

    plan = planner.plan(state, ContextRuntime(), tools=(tool.definition,))
    request_text = _input_texts(plan.request.input)
    compact_messages, _ = planner.compaction_view(state)
    compact_text = _input_texts(compact_messages)

    assert any("<system-reminder>" in text for text in request_text)
    assert any("The todo list may be stale" in text for text in compact_text)


def test_delivered_reminder_stays_at_its_runtime_history_position() -> None:
    history10 = _history_after_todo(10)
    attachment = TodoReminderAttachmentSource()(
        AttachmentDerivationState("session", history10, history10)
    )[0]
    reminder = AttachmentMessage(attachment, parent_uuid=history10[-1].uuid)
    later = _assistant(TextContent("after reminder"))
    history = history10 + (reminder, later)
    planner = ContextPlanner(
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
    )

    state = ContextPlanningState(history)
    items = planner.plan(
        state, ContextRuntime(), tools=(TodoWriteTool().definition,)
    ).request.input

    reminder_index = next(
        index
        for index, item in enumerate(items)
        if "The todo list may be stale" in "\n".join(_input_texts((item,)))
    )
    later_index = next(
        index
        for index, item in enumerate(items)
        if "after reminder" in _input_texts((item,))
    )
    assert reminder_index < later_index

    compact_messages, _ = planner.compaction_view(state)
    assert any(
        "The todo list may be stale" in text for text in _input_texts(compact_messages)
    )
