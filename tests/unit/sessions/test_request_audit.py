"""Semantic request audit persistence and corruption checks."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from my_code.conversation.models import HumanMessage
from my_code.model.display import DisplayDensity
from my_code.model.invocation import (
    ModelInputOrigin,
    ModelInputOriginKind,
    ModelInvocation,
    RequestPurpose,
)
from my_code.model.request import (
    InputText,
    ModelRequest,
    ModelToolDefinition,
    SystemPrompt,
    UserInput,
)
from my_code.sessions.session import Session


def _session(tmp_path: Path) -> Session:
    return Session(tmp_path, str(uuid4()))


def _invocation(*, request_id: str | None = None) -> ModelInvocation:
    request = ModelRequest(
        SystemPrompt.from_text("system"),
        (UserInput((InputText("hello"),)),),
        (
            ModelToolDefinition(
                "Read",
                "Read a file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        123,
    )
    return ModelInvocation(
        request=request,
        origins=(
            ModelInputOrigin(
                ModelInputOriginKind.USER_MESSAGE,
                source_id="message-1",
            ),
        ),
        purpose=RequestPurpose.AGENT,
        causal_head="message-1",
        step=1,
        request_id=request_id or str(uuid4()),
    )


def test_request_audit_deduplicates_blobs_and_resolves_exact_request(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    first = _invocation()
    second = _invocation()

    session.prepare_model_invocation(first)
    session.finish_model_invocation(first.request_id, "completed")
    session.prepare_model_invocation(second)
    session.finish_model_invocation(second.request_id, "failed", "offline")

    snapshot = session.request_audit_snapshot()
    assert not snapshot.legacy_missing
    assert [item.manifest.status for item in snapshot.requests] == [
        "completed",
        "failed",
    ]
    assert snapshot.requests[0].input == snapshot.requests[1].input
    assert snapshot.requests[0].manifest.input_refs == (
        snapshot.requests[1].manifest.input_refs
    )
    lines = [
        json.loads(line)
        for line in (tmp_path / session.session_id / "request-audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sum(line["type"] == "blob" for line in lines) == 3
    assert sum(line["type"] == "manifest" for line in lines) == 2


def test_legacy_session_restores_with_explicit_audit_gap(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.append_human_message(HumanMessage("legacy"))

    restored = Session.restore(tmp_path, session.session_id)

    assert restored.request_audit_snapshot().legacy_missing


def test_unfinished_request_restores_as_delivery_unknown(tmp_path: Path) -> None:
    session = _session(tmp_path)
    invocation = _invocation()
    session.prepare_model_invocation(invocation)

    restored = Session(tmp_path, session.session_id)

    assert restored.request_audit_snapshot().requests[0].manifest.status == (
        "delivery-unknown"
    )


def test_corrupt_request_audit_fails_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    invocation = _invocation()
    session.prepare_model_invocation(invocation)
    path = tmp_path / session.session_id / "request-audit.jsonl"
    records = path.read_text(encoding="utf-8").splitlines()
    blob = json.loads(records[0])
    blob["value"]["content"] = "tampered"
    records[0] = json.dumps(blob)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        Session(tmp_path, session.session_id)


def test_display_density_is_strictly_inclusive() -> None:
    assert DisplayDensity.CONCISE < DisplayDensity.DETAILED < DisplayDensity.AUDIT
    assert DisplayDensity.AUDIT.includes(DisplayDensity.DETAILED)
    assert DisplayDensity.DETAILED.includes(DisplayDensity.CONCISE)
    assert not DisplayDensity.CONCISE.includes(DisplayDensity.DETAILED)
