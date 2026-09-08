"""离线诊断必须保持原始证据不变，并明确重复检测的边界。"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
)
from my_code.conversation.presentation import generic_tool_result_presentation
from my_code.model.invocation import (
    ModelInputOrigin,
    ModelInputOriginKind,
    ModelInvocation,
    RequestPurpose,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import InputText, ModelRequest, SystemPrompt, UserInput
from my_code.sessions.diagnostics import build_diagnostic_report, request_evidence
from my_code.sessions.inspection import inspect_session
from my_code.sessions.session import Session


def _round(session: Session, name: str = "Read", *, close: bool = True) -> str:
    call = ToolCall(str(uuid4()), name, {"path": "private-file"})
    assistant = AssistantMessage(
        (call,), TokenUsage(1, 2, 3, 4, True), parent_uuid=session.causal_head_uuid
    )
    session.append_assistant_message(assistant)
    if close:
        session.append_tool_results(
            (
                ToolResult(
                    call.id,
                    "private error",
                    generic_tool_result_presentation("error", True),
                    True,
                ),
            ),
            assistant,
        )
    return call.id


def _session(tmp_path: Path) -> Session:
    session = Session(tmp_path, str(uuid4()))
    session.append_human_message(HumanMessage("private prompt"))
    return session


def test_inspection_does_not_repair_unresolved_calls_or_write_files(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    call_id = _round(session, close=False)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    snapshot = inspect_session(tmp_path, session.session_id)
    report = build_diagnostic_report(snapshot)

    assert any(
        item["kind"] == "unresolved_tool_call" and item["tool_call_id"] == call_id
        for item in report["findings"]  # type: ignore[union-attr, index]  # JSON 报告结构由本测试验证。
    )
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("pattern", [("Read",), ("Read", "Bash")])
def test_report_detects_repeated_rounds_and_tool_errors(
    tmp_path: Path, pattern
) -> None:
    session = _session(tmp_path)
    for _ in range(3):
        for name in pattern:
            _round(session, name)

    snapshot = inspect_session(tmp_path, session.session_id)
    report = build_diagnostic_report(snapshot)
    findings = report["findings"]
    assert isinstance(findings, list)
    repeats = [
        item
        for item in findings
        if isinstance(item, dict) and item["kind"] == "repeated_tool_rounds"
    ]
    assert len(repeats) == 1
    assert repeats[0]["period"] == len(pattern)
    assert repeats[0]["repetitions"] == 3
    assert "private error" not in json.dumps(report)
    assert "private error" in json.dumps(
        build_diagnostic_report(snapshot, include_content=True)
    )


def test_repetition_does_not_cross_user_turns(tmp_path: Path) -> None:
    session = _session(tmp_path)
    for _ in range(3):
        _round(session)
        session.append_human_message(
            HumanMessage("again", parent_uuid=session.causal_head_uuid)
        )
    report = build_diagnostic_report(inspect_session(tmp_path, session.session_id))
    assert "repeated_tool_rounds" not in json.dumps(report)


def test_request_evidence_is_explicit_and_unknown_delivery_is_visible(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    invocation = ModelInvocation(
        ModelRequest(
            SystemPrompt.from_text("private system"),
            (UserInput((InputText("private input"),)),),
            (),
            10,
        ),
        (ModelInputOrigin(ModelInputOriginKind.USER_MESSAGE),),
        RequestPurpose.AGENT,
        session.causal_head_uuid,
        1,
    )
    session.prepare_model_invocation(invocation)
    snapshot = inspect_session(tmp_path, session.session_id)
    assert "delivery-unknown" in json.dumps(build_diagnostic_report(snapshot))
    assert "private input" in json.dumps(
        request_evidence(snapshot, invocation.request_id)
    )
    with pytest.raises(ValueError, match="Unknown request"):
        request_evidence(snapshot, "missing")


def test_inspection_rejects_missing_session_without_creating_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inspect_session(tmp_path, str(uuid4()))
    assert list(tmp_path.iterdir()) == []
