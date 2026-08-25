from pathlib import Path

import pytest

from my_code.context.attachments.sources import DerivedAttachmentResolver
from my_code.context.documents import ContextInstruction
from my_code.context.planner import ContextPlanner
from my_code.context.session import AttachmentDelivery
from my_code.context.session import ContextSnapshot as ConversationSnapshot
from my_code.context.window import ContextWindow
from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
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
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.tools.catalog import ToolCatalogSnapshot
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
            content=(ToolResult("todo-1", "updated"),),
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
    assert outcome.presentation.summary == "Updated 1 todo(s)"


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


def test_failed_todo_write_does_not_replace_last_successful_state() -> None:
    history = list(_history_after_todo(2))
    failed = _assistant(ToolCall("todo-failed", "TodoWrite", {"todos": "invalid"}))
    history.extend(
        (
            failed,
            ToolResultBatch(
                content=(ToolResult("todo-failed", "invalid", is_error=True),),
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
    assert source(ConversationSnapshot(_history_after_todo(9))) == ()

    attachments = source(ConversationSnapshot(history10, session_history=history10))
    assert len(attachments) == 1
    assert attachments[0].source == "todo_reminder"
    instruction = attachments[0].content[0]
    assert isinstance(instruction, ContextInstruction)
    assert "1. [in_progress] Run tests" in instruction.content
    assert "NEVER mention this reminder" in instruction.content
    assert attachments[0].retention == "live_session"

    delivery = AttachmentDelivery(history10[-1].uuid, attachments[0])
    history11 = history10 + (_assistant(TextContent("call 11")),)
    assert (
        source(
            ConversationSnapshot(
                history11,
                session_history=history11,
                attachment_deliveries=(delivery,),
            )
        )
        == ()
    )

    history20 = history10 + tuple(
        _assistant(TextContent(f"later {index}")) for index in range(10)
    )
    assert (
        len(
            source(
                ConversationSnapshot(
                    history20,
                    session_history=history20,
                    attachment_deliveries=(delivery,),
                )
            )
        )
        == 1
    )

    # reminder 不持久化：恢复时没有 delivery history，已经超过阈值会立即提醒，
    # 而不是等到 turns_since_write 恰好能被 10 整除。
    history15 = history10 + tuple(
        _assistant(TextContent(f"resumed {index}")) for index in range(5)
    )
    assert len(source(ConversationSnapshot(history15, session_history=history15))) == 1


def test_todo_reminder_uses_full_session_history_after_compaction() -> None:
    history = _history_after_todo(10)
    compact_working_set = tuple(
        _assistant(TextContent(f"post-compact {index}")) for index in range(10)
    )

    attachments = TodoReminderAttachmentSource()(
        ConversationSnapshot(
            compact_working_set,
            session_history=history,
        )
    )

    assert len(attachments) == 1
    instruction = attachments[0].content[0]
    assert isinstance(instruction, ContextInstruction)
    assert "Run tests" in instruction.content


def test_todo_reminder_without_prior_write_starts_after_ten_model_calls() -> None:
    history = tuple(_assistant(TextContent(str(index))) for index in range(10))

    attachments = TodoReminderAttachmentSource()(
        ConversationSnapshot(history, session_history=history)
    )

    assert len(attachments) == 1
    instruction = attachments[0].content[0]
    assert isinstance(instruction, ContextInstruction)
    assert "existing contents" not in instruction.content


def test_context_planner_attaches_reminder_but_compaction_excludes_it() -> None:
    history = _history_after_todo(10)
    tool = TodoWriteTool()
    planner = ContextPlanner(
        window=ContextWindow(20_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
        attachment_resolver=DerivedAttachmentResolver(
            (TodoReminderAttachmentSource(),)
        ),
    )
    snapshot = ConversationSnapshot(history, session_history=history)

    plan = planner.plan(snapshot, tools=(tool.definition,))
    request_text = _input_texts(plan.request.input)
    compact_messages, _ = planner.compaction_view(snapshot)
    compact_text = _input_texts(compact_messages)

    assert any("<system-reminder>" in text for text in request_text)
    assert len(plan.new_attachment_deliveries) == 1
    assert not any("TodoWrite tool hasn't been used" in text for text in compact_text)


def test_delivered_reminder_stays_at_its_runtime_history_position() -> None:
    history10 = _history_after_todo(10)
    attachment = TodoReminderAttachmentSource()(
        ConversationSnapshot(history10, session_history=history10)
    )[0]
    delivery = AttachmentDelivery(history10[-1].uuid, attachment)
    later = _assistant(TextContent("after reminder"))
    history = history10 + (later,)
    planner = ContextPlanner(
        window=ContextWindow(20_000),
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
        attachment_resolver=DerivedAttachmentResolver(
            (TodoReminderAttachmentSource(),)
        ),
    )

    snapshot = ConversationSnapshot(
        history,
        session_history=history,
        attachment_deliveries=(delivery,),
    )
    items = planner.plan(snapshot, tools=(TodoWriteTool().definition,)).request.input

    reminder_index = next(
        index
        for index, item in enumerate(items)
        if "TodoWrite tool hasn't been used" in "\n".join(_input_texts((item,)))
    )
    later_index = next(
        index
        for index, item in enumerate(items)
        if "after reminder" in _input_texts((item,))
    )
    assert reminder_index < later_index

    compact_messages, _ = planner.compaction_view(snapshot)
    assert any(
        "TodoWrite tool hasn't been used" in text
        for text in _input_texts(compact_messages)
    )
