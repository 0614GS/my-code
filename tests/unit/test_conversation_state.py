from pathlib import Path

import pytest

from nano_code.context.attachments.models import ContextAttachment
from nano_code.context.documents import ContextInstruction
from nano_code.context.normalization import ModelInputNormalizer
from nano_code.context.session import AttachmentDelivery, ContextSession
from nano_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultsMessage,
)
from nano_code.conversation.state import CompactBoundary, ContentReplacement
from nano_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
)
from nano_code.model.request import ModelReasoningBlock
from nano_code.sessions.models import SessionSnapshot
from nano_code.sessions.session import Session
from nano_code.sessions.store import SessionStore


class CountingSessionStore(SessionStore):
    def __init__(self, project_state_dir: Path, session_id: str) -> None:
        super().__init__(project_state_dir, session_id)
        self.load_calls = 0

    def load(self) -> SessionSnapshot:
        self.load_calls += 1
        return super().load()


def _store(tmp_path: Path, suffix: str) -> SessionStore:
    return SessionStore(tmp_path, f"00000000-0000-0000-0000-{suffix:0>12}")


def test_append_is_persisted_then_applied_without_runtime_reload(
    tmp_path: Path,
) -> None:
    store = CountingSessionStore(tmp_path, "00000000-0000-0000-0000-000000000001")
    session = Session(store)
    message = HumanMessage("hello")
    session.append(message)
    assert session.history == (message,)
    assert session.working_messages == (message,)
    assert store.load_calls == 1


def test_append_tool_results_requires_a_result(tmp_path: Path) -> None:
    session = Session(_store(tmp_path, "2"))
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    session.append(human)
    session.append(assistant)
    with pytest.raises(ValueError, match="at least one"):
        session.append_tool_results((), assistant)


def test_restore_repairs_trailing_tool_calls_before_returning(tmp_path: Path) -> None:
    target = CountingSessionStore(tmp_path, "00000000-0000-0000-0000-000000000003")
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
    target.load_calls = 0

    resumed = Session.restore(target)
    assert isinstance(resumed.history[-1], ToolResultsMessage)
    assert resumed.history[-1].content[0].is_error is True
    assert target.load_calls == 1
    request_messages = ModelInputNormalizer().normalize((), resumed.history, ())
    assert any(
        isinstance(block, ModelReasoningBlock)
        for message in request_messages
        for block in message.content
    )


def test_empty_or_repair_failure_does_not_replace_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = Session(_store(tmp_path, "4"))
    current.append(HumanMessage("current"))
    active = current
    with pytest.raises(ValueError, match="no messages"):
        Session.restore(_store(tmp_path, "5"))
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

    def fail(_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(target, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        Session.restore(target)
    assert active is current
    assert active.history[-1] == current.history[-1]


def test_failed_persistence_does_not_change_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "7")
    session = Session(store)

    def fail(_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        session.append(HumanMessage("not durable"))
    assert session.history == ()


def test_compaction_is_persisted_before_conversation_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "8")
    session = Session(store)
    human = HumanMessage("hello")
    session.append(human)
    summary = ConversationSummaryMessage("summary", parent_uuid=human.uuid)
    boundary = CompactBoundary(human.uuid, summary.uuid, "manual", 5)
    replacement = ContentReplacement("call", "Read", 10, "short")

    def fail(_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        session.commit_compaction((replacement,), summary, boundary)
    assert session.history == (human,)
    assert session.conversation.all_content_replacements == ()
    assert session.compact_boundaries == ()


def test_compaction_updates_working_set_without_reload(tmp_path: Path) -> None:
    store = CountingSessionStore(tmp_path, "00000000-0000-0000-0000-000000000009")
    session = Session(store)
    human = HumanMessage("hello")
    session.append(human)
    summary = ConversationSummaryMessage("summary", parent_uuid=human.uuid)
    boundary = CompactBoundary(human.uuid, summary.uuid, "manual", 5)
    session.commit_compaction((), summary, boundary)
    assert session.history == (human, summary)
    assert session.working_messages == (summary,)
    assert store.load_calls == 1


def test_context_session_owns_ephemeral_attachment_delivery(tmp_path: Path) -> None:
    session = Session(_store(tmp_path, "10"))
    human = HumanMessage("current")
    session.append(human)
    context = ContextSession()
    reminder = ContextAttachment(
        "todo_reminder",
        (ContextInstruction("remember todos"),),
        retention="live_session",
    )
    delivery = AttachmentDelivery(human.uuid, reminder, delivery_id="fixed")
    context.add((delivery, delivery), session.conversation.snapshot())
    assert context.snapshot(session.conversation.snapshot()).attachment_deliveries == (
        delivery,
    )
    assert session.store.load().history == (human,)
    assert (
        ContextSession().snapshot(session.conversation.snapshot()).attachment_deliveries
        == ()
    )


def test_external_transcript_append_is_visible_only_after_new_session(
    tmp_path: Path,
) -> None:
    session_id = "00000000-0000-0000-0000-000000000012"
    session = Session(SessionStore(tmp_path, session_id))
    human = HumanMessage("local")
    session.append(human)
    external = AssistantMessage(
        (TextContent("external"),), TokenUsage(), parent_uuid=human.uuid
    )
    SessionStore(tmp_path, session_id).append(external)
    assert session.history == (human,)
    assert Session(SessionStore(tmp_path, session_id)).history == (human, external)


def test_context_session_rejects_conflicting_delivery_id(tmp_path: Path) -> None:
    session = Session(_store(tmp_path, "13"))
    human = HumanMessage("current")
    session.append(human)
    context = ContextSession()
    first = AttachmentDelivery(
        human.uuid,
        ContextAttachment("event", (TextContent("first"),), retention="live_session"),
        delivery_id="fixed",
    )
    context.add((first,), session.conversation.snapshot())
    conflict = AttachmentDelivery(
        human.uuid,
        ContextAttachment("event", (TextContent("second"),), retention="live_session"),
        delivery_id="fixed",
    )
    with pytest.raises(ValueError, match="Conflicting attachment delivery"):
        context.add((conflict,), session.conversation.snapshot())
    assert context.snapshot(session.conversation.snapshot()).attachment_deliveries == (
        first,
    )


def test_attachment_delivery_rejects_bad_anchor_and_retention(tmp_path: Path) -> None:
    session = Session(_store(tmp_path, "11"))
    session.append(HumanMessage("current"))
    context = ContextSession()
    delivery = AttachmentDelivery(
        "missing",
        ContextAttachment("event", (TextContent("content"),), retention="live_session"),
    )
    with pytest.raises(ValueError, match="not in the working set"):
        context.add((delivery,), session.conversation.snapshot())
    with pytest.raises(ValueError, match="live_session"):
        AttachmentDelivery(
            "anchor", ContextAttachment("temporary", (TextContent("content"),))
        )
