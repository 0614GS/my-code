import pytest

from nano_code.context import ContextWindow
from nano_code.messages import ChatMessage, TextBlock, ToolResultBlock, ToolUseBlock


def user(text: str, parent: str | None = None) -> ChatMessage:
    return ChatMessage(
        role="user",
        origin="human",
        content=(TextBlock(text),),
        parent_uuid=parent,
    )


def test_projection_drops_only_at_human_turn_boundary() -> None:
    first = user("old prompt")
    first_answer = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("old answer"),),
        parent_uuid=first.uuid,
    )
    current = user("new", first_answer.uuid)
    tool_use = ChatMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("call", "Read", {"path": "x"}),),
        parent_uuid=current.uuid,
    )
    tool_result = ChatMessage(
        role="user",
        origin="tool",
        content=(ToolResultBlock("call", "value"),),
        parent_uuid=tool_use.uuid,
    )

    projected = ContextWindow(max_chars=40).project(
        (first, first_answer, current, tool_use, tool_result)
    )

    assert projected == (current, tool_use, tool_result)


def test_projection_rejects_orphan_tool_result() -> None:
    prompt = user("new")
    result = ChatMessage(
        role="user",
        origin="tool",
        content=(ToolResultBlock("missing", "value"),),
        parent_uuid=prompt.uuid,
    )

    with pytest.raises(ValueError, match="Orphan tool result"):
        ContextWindow().project((prompt, result))
