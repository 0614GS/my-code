from pathlib import Path

import pytest

from my_code.context.normalization import ModelInputNormalizer
from my_code.conversation.attachments import (
    FileMentionAttachment,
    TodoReminderAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.state import CompactBoundary, ContentReplacement
from my_code.model.primitives import (
    ProviderBinding,
    ProviderContinuationState,
    ProviderReplayRecord,
    ReasoningPresentation,
    TokenUsage,
    replay_content_id,
)
from my_code.model.request import AssistantOutput, ModelReasoningBlock
from my_code.sessions._store import SessionStore
from my_code.sessions.models import SessionSnapshot
from my_code.sessions.session import Session


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


def test_session_externalizes_large_tool_result_during_commit(tmp_path: Path) -> None:
    session = _session(tmp_path, "20")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("large-call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    session.append_human_message(human)
    session.append_assistant_message(assistant)

    persisted = session.append_tool_results(
        (ToolResult("large-call", "x" * 20_001),), assistant
    )

    result = persisted.content[0]
    assert "Output exceeded 20000 characters" in result.content
    result_files = tuple((tmp_path / session.session_id / "tool-results").iterdir())
    assert len(result_files) == 1
    assert result_files[0].read_text(encoding="utf-8") == "x" * 20_001
    assert (
        Session.restore(tmp_path, session.session_id).snapshot().history[-1]
        == persisted
    )


def test_failed_tool_result_commit_rolls_back_externalized_file_and_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, "21")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (ToolCall("large-call", "Read", {"path": "x"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    session.append_human_message(human)
    session.append_assistant_message(assistant)
    before = session.snapshot()

    def fail(_store: SessionStore, _records: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        session.append_tool_results(
            (ToolResult("large-call", "x" * 20_001),), assistant
        )

    assert session.snapshot() == before
    result_dir = tmp_path / session.session_id / "tool-results"
    assert not result_dir.exists() or not tuple(result_dir.iterdir())


def test_restore_repairs_trailing_tool_calls_before_returning(tmp_path: Path) -> None:
    target = _store(tmp_path, "3")
    human = HumanMessage("read")
    continuation = ProviderContinuationState(
        ProviderBinding("anthropic-messages", "anthropic", "claude-test"),
        "active_trajectory",
        {"type": "thinking", "thinking": "hidden", "signature": "signed"},
    )
    assistant = AssistantMessage(
        (
            ReasoningContent(
                "thinking",
                ReasoningPresentation("verbatim", ("hidden",)),
            ),
            TextContent("working"),
            ToolCall("call", "Read", {"path": "x"}),
        ),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    target.append(human)
    target.append_message(
        assistant,
        replay_records=(
            ProviderReplayRecord(assistant.uuid, replay_content_id(0), continuation),
        ),
    )
    resumed = Session.restore(tmp_path, target.session_id)
    history = resumed.snapshot().history
    assert isinstance(history[-1], ToolResultBatch)
    assert history[-1].content[0].is_error is True
    snapshot = resumed.context_snapshot()
    request_messages = ModelInputNormalizer().normalize(
        (), history, snapshot.replay_records
    )
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


def test_compaction_prunes_replay_from_context_but_preserves_recoverable_sidecar(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, "22")
    human = HumanMessage("hello")
    assistant = AssistantMessage(
        (TextContent("answer"),), TokenUsage(), parent_uuid=human.uuid
    )
    replay = ProviderReplayRecord(
        assistant.uuid,
        replay_content_id(0),
        ProviderContinuationState(
            ProviderBinding("openai-responses", "openai", "gpt-test"),
            "working_context",
            {"type": "message", "id": "msg", "role": "assistant", "content": []},
        ),
    )
    session.append_human_message(human)
    session.append_assistant_message(assistant, replay_records=(replay,))
    summary = ConversationSummaryMessage("state", parent_uuid=assistant.uuid)
    boundary = CompactBoundary(assistant.uuid, summary.uuid, "manual", 1)

    session.commit_compaction((), summary, boundary)

    assert session.snapshot().replay_records == (replay,)
    assert session.context_snapshot().replay_records == ()


def test_transient_attachment_is_memory_only_and_skips_parent_chain(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, "10")
    human = HumanMessage("current")
    session.append_human_message(human)
    reminder = session.append_attachment(TodoReminderAttachment("remember todos"))
    assistant = AssistantMessage(
        (TextContent("done"),), TokenUsage(), parent_uuid=session.causal_head_uuid
    )
    session.append_assistant_message(assistant)
    assert session.snapshot().history == (human, reminder, assistant)
    assert assistant.parent_uuid == human.uuid
    assert Session(tmp_path, session.session_id).snapshot().history == (
        human,
        assistant,
    )


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


def test_durable_attachment_round_trips_in_parent_chain(tmp_path: Path) -> None:
    session = _session(tmp_path, "13")
    human = HumanMessage("current")
    session.append_human_message(human)
    attachment = session.append_attachment(FileMentionAttachment("a.txt", "first"))
    assert attachment.parent_uuid == human.uuid
    assert Session(tmp_path, session.session_id).snapshot().history == (
        human,
        attachment,
    )


def test_attachment_cannot_split_tool_call_and_result(tmp_path: Path) -> None:
    session = _session(tmp_path, "11")
    human = HumanMessage("current")
    assistant = AssistantMessage(
        (ToolCall("call", "Read", {"path": "a"}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    session.append_human_message(human)
    session.append_assistant_message(assistant)
    with pytest.raises(ValueError, match="between assistant tool calls"):
        session.append_attachment(FileMentionAttachment("a", "content"))
