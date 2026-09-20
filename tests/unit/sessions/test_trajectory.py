from pathlib import Path
from typing import Any
from uuid import uuid4

from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
)
from my_code.conversation.presentation import generic_tool_result_presentation
from my_code.model.primitives import ReasoningPresentation, TokenUsage
from my_code.sessions.inspection import inspect_session
from my_code.sessions.session import Session
from my_code.sessions.trajectory import export_session_trajectory


def test_export_preserves_reasoning_tools_cache_and_evidence_gaps(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, str(uuid4()))
    human = HumanMessage("fix it")
    session.append_human_message(human)
    call = ToolCall("call-1", "Read", {"path": "a.py"})
    assistant = AssistantMessage(
        (
            ReasoningContent(
                "reasoning-1", ReasoningPresentation("summary", ("inspect",))
            ),
            TextContent("working"),
            call,
        ),
        TokenUsage(10, 4, 3, 2, True),
        parent_uuid=human.uuid,
    )
    session.append_assistant_message(assistant)
    session.append_tool_results(
        (
            ToolResult(
                call.id,
                "contents",
                generic_tool_result_presentation("read", False),
            ),
        ),
        assistant,
    )

    dto: Any = export_session_trajectory(inspect_session(tmp_path, session.session_id))

    agent = dto["steps"][1]
    assert isinstance(agent, dict)
    assert agent["reasoning"] == "inspect"
    assert agent["usage"] == {
        "prompt_tokens": 15,
        "completion_tokens": 4,
        "cache_read_tokens": 2,
        "cache_write_tokens": 3,
        "provider_reported": True,
    }
    observation = dto["steps"][2]
    assert isinstance(observation, dict)
    assert observation["results"][0]["call_id"] == "call-1"
    assert {item["kind"] for item in dto["evidence_gaps"]} >= {"missing_request_audit"}
