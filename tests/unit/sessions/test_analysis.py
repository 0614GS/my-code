"""确定性 Session 分析只依赖原生证据并默认隐藏正文。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
)
from my_code.conversation.presentation import generic_tool_result_presentation
from my_code.foundation.json import JsonObject
from my_code.model.primitives import TokenUsage
from my_code.sessions.analysis import analyze_session, classify_tool_failure
from my_code.sessions.inspection import inspect_session
from my_code.sessions.session import Session


def _append_call(
    session: Session,
    call_id: str,
    name: str,
    tool_input: JsonObject,
    content: str,
    *,
    is_error: bool,
    usage: TokenUsage | None = None,
) -> None:
    call = ToolCall(call_id, name, tool_input)
    assistant = AssistantMessage(
        (call,),
        usage or TokenUsage(1, 2, 3, 4, True),
        parent_uuid=session.causal_head_uuid,
    )
    session.append_assistant_message(assistant)
    session.append_tool_results(
        (
            ToolResult(
                call_id,
                content,
                generic_tool_result_presentation(content, is_error),
                is_error,
            ),
        ),
        assistant,
    )


def test_analysis_classifies_tools_and_preserves_privacy(tmp_path: Path) -> None:
    session = Session(tmp_path, str(uuid4()))
    session.append_human_message(HumanMessage("private prompt"))
    _append_call(
        session,
        "edit-1",
        "Edit",
        {"path": "private.py", "old_string": "secret"},
        "ToolExecutionError: Read the entire current file before editing it",
        is_error=True,
    )
    _append_call(
        session,
        "edit-2",
        "Edit",
        {"path": "private.py", "old_string": "secret"},
        "edited",
        is_error=False,
    )
    timeline = {
        "records": [
            {
                "event": "tool.execution.finished",
                "tool_call_id": "edit-1",
                "duration_ms": 4.5,
                "dropped_events": 0,
            }
        ],
        "files_scanned": 1,
        "malformed_records": 0,
    }

    report = analyze_session(inspect_session(tmp_path, session.session_id), timeline)

    assert report["usage"]["total_input_tokens"] == 16  # type: ignore[index]
    assert report["usage"]["cache_hit_ratio"] == 0.5  # type: ignore[index]
    assert report["tools"]["emitted"] == 2  # type: ignore[index]
    assert report["tools"]["by_category"] == {  # type: ignore[index]
        "precondition_failed": 1
    }
    first = report["tool_calls"][0]  # type: ignore[index]
    assert first["duration_ms"] == 4.5  # type: ignore[index]
    assert first["exact_retry_succeeded"] is True  # type: ignore[index]
    assert "private.py" not in json.dumps(report)


def test_permission_event_overrides_result_text(tmp_path: Path) -> None:
    session = Session(tmp_path, str(uuid4()))
    session.append_human_message(HumanMessage("read"))
    _append_call(
        session,
        "read-1",
        "Read",
        {"path": "/tmp/private"},
        "arbitrary presentation",
        is_error=True,
    )
    timeline = {
        "records": [
            {
                "event": "tool.permission.evaluated",
                "tool_call_id": "read-1",
                "behavior": "deny",
                "reason_kind": "safety",
                "authority": "use_default",
                "execution_backend": "local",
                "dropped_events": 0,
            }
        ],
        "files_scanned": 1,
        "malformed_records": 0,
    }

    report = analyze_session(inspect_session(tmp_path, session.session_id), timeline)

    assert report["tools"]["permission_denials"] == 1  # type: ignore[index]
    assert report["tools"]["by_category"] == {  # type: ignore[index]
        "permission_denied": 1
    }


def test_missing_diagnostics_is_an_explicit_gap(tmp_path: Path) -> None:
    session = Session(tmp_path, str(uuid4()))
    session.append_human_message(HumanMessage("hello"))

    report = analyze_session(inspect_session(tmp_path, session.session_id))

    quality = report["evidence_quality"]  # type: ignore[index]
    assert "diagnostic_timeline_not_loaded" in quality["evidence_gaps"]  # type: ignore[index]


def test_malformed_diagnostics_remain_a_partial_report(tmp_path: Path) -> None:
    session = Session(tmp_path, str(uuid4()))
    session.append_human_message(HumanMessage("hello"))

    report = analyze_session(
        inspect_session(tmp_path, session.session_id),
        {
            "records": [],
            "files_scanned": 1,
            "malformed_records": 2,
            "evidence_gap": "diagnostic_records_incomplete",
        },
    )

    quality = report["evidence_quality"]  # type: ignore[index]
    assert quality["malformed_diagnostic_records"] == 2  # type: ignore[index]
    gaps = quality["evidence_gaps"]  # type: ignore[index]
    assert isinstance(gaps, list)
    assert "diagnostic_records_incomplete" in gaps
    assert "diagnostic_records_malformed" in gaps


def test_failure_rules_keep_command_exit_separate() -> None:
    invalid = classify_tool_failure("Read", "Invalid input: 'path' must be a string")
    command = classify_tool_failure("Bash", "exit_code: 1\ntests failed")

    assert invalid["category"] == "invalid_input"
    assert command["category"] == "command_nonzero"
