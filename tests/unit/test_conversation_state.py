from pathlib import Path

import pytest

from my_code.context.attachments.models import ContextAttachment
from my_code.context.documents import ContextInstruction
from my_code.context.normalization import ModelInputNormalizer
from my_code.context.session import AttachmentDelivery, ContextSession
from my_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultBatch,
)
from my_code.conversation.state import CompactBoundary, ContentReplacement
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
)
from my_code.model.request import AssistantOutput, ModelReasoningBlock
from my_code.sessions.models import SessionSnapshot
from my_code.sessions.session import Session
from my_code.sessions.store import SessionStore


def _store(tmp_path: Path, suffix: str) -> SessionStore:
    return SessionStore(tmp_path, f"00000000-0000-0000-0000-{suffix:0>12}")


def _session(tmp_path: Path, suffix: str) -> Session:
    return Session(tmp_path, f"00000000-0000-0000-0000-{suffix:0>12}")


def test_append_is_persisted_then_applied_without_runtime_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = SessionStore.load
    calls = 0

    def count(store: SessionStore) -> SessionSnapshot:
        nonlocal calls
        calls += 1
        return load(store)

    monkeypatch.setattr(SessionStore, "load", count)
    session = _session(tmp_path, "1")
    message = HumanMessage("hello")
    session.append_human_message(message)
    assert session.snapshot().history == (message,)
    assert session.snapshot().working_set == (message,)
    assert calls == 1


def test_append_tool_results_requires_a_result(tmp_path: Path) -> None:
    session = _session(tmp_path, "2")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    session.append_human_message(human)
    session.append_assistant_message(assistant)
    with pytest.raises(ValueError, match="at least one"):
        session.append_tool_results((), assistant)


def test_restore_repairs_trailing_tool_calls_before_returning(tmp_path: Path) -> None:
    target = _store(tmp_path, "3")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (
            ReasoningContent(
                "thinking",
                ReasoningPresentation("verbatim", ("hidden",)),
                ProviderContinuationState(
                    ProviderBinding("anthropic-messages", "anthropic", "claude-test"),
                    "active_trajectory",
                    {"type": "thinking", "thinking": "hidden", "signature": "signed"},
                ),
            ),
            TextContent("working"),
            ToolCall("call", "Read", {"path": "x"}),
        ),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    target.append(human)
    target.append(assistant)
    resumed = Session.restore(tmp_path, target.session_id)
    history = resumed.snapshot().history
    assert isinstance(history[-1], ToolResultBatch)
    assert history[-1].content[0].is_error is True
    request_messages = ModelInputNormalizer().normalize((), history, ())
    assert any(
        isinstance(block, ModelReasoningBlock)
        for item in request_messages
        if isinstance(item, AssistantOutput)
        for block in item.content
    )


def test_empty_or_repair_failure_does_not_replace_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = _session(tmp_path, "4")
    current.append_human_message(HumanMessage("current"))
    active = current
    with pytest.raises(ValueError, match="no messages"):
        Session.restore(tmp_path, _store(tmp_path, "5").session_id)
    assert active is current

    target = _store(tmp_path, "6")
    human = HumanMessage("read")
    target.append(human)
    target.append(
        AssistantMessage(
            (ToolCall("call", "Read", {"path": "x"}),),
            TokenUsage(),
            parent_uuid=human.uuid,
        )
    )

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        Session.restore(tmp_path, target.session_id)
    assert active is current
    assert active.snapshot().history[-1] == current.snapshot().history[-1]


def test_failed_persistence_does_not_change_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "7")
    session = Session(tmp_path, store.session_id)

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        session.append_human_message(HumanMessage("not durable"))
    assert session.snapshot().history == ()


def test_compaction_is_persisted_before_conversation_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "8")
    session = Session(tmp_path, store.session_id)
    human = HumanMessage("hello")
    session.append_human_message(human)
    summary = ConversationSummaryMessage("summary", parent_uuid=human.uuid)
    boundary = CompactBoundary(human.uuid, summary.uuid, "manual", 5)
    replacement = ContentReplacement("call", "Read", 10, "short")

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        session.commit_compaction((replacement,), summary, boundary)
    snapshot = session.snapshot()
    assert snapshot.history == (human,)
    assert snapshot.content_replacements == ()
    assert snapshot.compact_boundaries == ()


def test_compaction_updates_working_set_without_reload(tmp_path: Path) -> None:
    session = _session(tmp_path, "9")
    human = HumanMessage("hello")
    session.append_human_message(human)
    summary = ConversationSummaryMessage("summary", parent_uuid=human.uuid)
    boundary = CompactBoundary(human.uuid, summary.uuid, "manual", 5)
    session.commit_compaction((), summary, boundary)
    assert session.snapshot().history == (human, summary)
    assert session.snapshot().working_set == (summary,)


def test_context_session_owns_ephemeral_attachment_delivery(tmp_path: Path) -> None:
    session = _session(tmp_path, "10")
    human = HumanMessage("current")
    session.append_human_message(human)
    context = ContextSession()
    reminder = ContextAttachment(
        "todo_reminder",
        (ContextInstruction("remember todos"),),
        retention="live_session",
    )
    delivery = AttachmentDelivery(human.uuid, reminder, delivery_id="fixed")
    context.add((delivery, delivery), session.snapshot())
    assert context.snapshot(session.snapshot()).attachment_deliveries == (delivery,)
    assert _store(tmp_path, "10").load().history == (human,)
    assert ContextSession().snapshot(session.snapshot()).attachment_deliveries == ()


def test_external_transcript_append_is_visible_only_after_new_session(
    tmp_path: Path,
) -> None:
    session_id = "00000000-0000-0000-0000-000000000012"
    session = Session(tmp_path, session_id)
    human = HumanMessage("local")
    session.append_human_message(human)
    external = AssistantMessage(
        (TextContent("external"),), TokenUsage(), parent_uuid=human.uuid
    )
    SessionStore(tmp_path, session_id).append(external)
    assert session.snapshot().history == (human,)
    assert Session(tmp_path, session_id).snapshot().history == (human, external)


def test_context_session_rejects_conflicting_delivery_id(tmp_path: Path) -> None:
    session = _session(tmp_path, "13")
    human = HumanMessage("current")
    session.append_human_message(human)
    context = ContextSession()
    first = AttachmentDelivery(
        human.uuid,
        ContextAttachment("event", (TextContent("first"),), retention="live_session"),
        delivery_id="fixed",
    )
    context.add((first,), session.snapshot())
    conflict = AttachmentDelivery(
        human.uuid,
        ContextAttachment("event", (TextContent("second"),), retention="live_session"),
        delivery_id="fixed",
    )
    with pytest.raises(ValueError, match="Conflicting attachment delivery"):
        context.add((conflict,), session.snapshot())
    assert context.snapshot(session.snapshot()).attachment_deliveries == (first,)


def test_attachment_delivery_rejects_bad_anchor_and_retention(tmp_path: Path) -> None:
    session = _session(tmp_path, "11")
    session.append_human_message(HumanMessage("current"))
    context = ContextSession()
    delivery = AttachmentDelivery(
        "missing",
        ContextAttachment("event", (TextContent("content"),), retention="live_session"),
    )
    with pytest.raises(ValueError, match="not in the working set"):
        context.add((delivery,), session.snapshot())
    with pytest.raises(ValueError, match="live_session"):
        AttachmentDelivery(
            "anchor", ContextAttachment("temporary", (TextContent("content"),))
        )
