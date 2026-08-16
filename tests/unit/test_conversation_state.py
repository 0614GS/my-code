from pathlib import Path

import pytest

from nano_code.agent import ConversationState
from nano_code.messages import (
    AssistantMessage,
    HumanMessage,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResultsMessage,
)
from nano_code.sessions import SessionStore


def _store(tmp_path: Path, suffix: str) -> SessionStore:
    return SessionStore(tmp_path, f"00000000-0000-0000-0000-{suffix:0>12}")


def test_append_is_persisted_before_runtime_refresh(tmp_path: Path) -> None:
    state = ConversationState(_store(tmp_path, "1"))
    message = HumanMessage("hello")
    state.append(message)
    assert state.history == (message,)
    assert state.working_messages == (message,)


def test_append_tool_results_builds_semantic_message(tmp_path: Path) -> None:
    state = ConversationState(_store(tmp_path, "2"))
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    state.append(human)
    state.append(assistant)
    with pytest.raises(ValueError, match="at least one"):
        state.append_tool_results((), assistant)


def test_resume_repairs_trailing_tool_calls(tmp_path: Path) -> None:
    target = _store(tmp_path, "3")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (TextContent("working"), ToolCall("call", "Read", {"path": "x"})),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    target.append(human)
    target.append(assistant)

    state = ConversationState(_store(tmp_path, "4"))
    resumed = state.resume(target)

    assert isinstance(resumed[-1], ToolResultsMessage)
    assert resumed[-1].source_assistant_uuid == assistant.uuid
    assert resumed[-1].content[0].is_error is True


def test_resume_empty_session_keeps_current_repository(tmp_path: Path) -> None:
    current = _store(tmp_path, "5")
    current.append(HumanMessage("current"))
    state = ConversationState(current)
    with pytest.raises(ValueError, match="no messages"):
        state.resume(_store(tmp_path, "6"))
    assert state.session_id == current.session_id
