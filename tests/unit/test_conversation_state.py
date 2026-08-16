from pathlib import Path

import pytest

from nano_code.agent import ConversationState
from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.session import (
    CompactBoundary,
    ContentReplacement,
    SessionSnapshot,
)
from nano_code.messages import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResultsMessage,
)
from nano_code.sessions import SessionStore


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
    state = ConversationState(store)
    message = HumanMessage("hello")
    state.append(message)
    assert state.history == (message,)
    assert state.working_messages == (message,)
    assert store.load_calls == 1


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
    target = CountingSessionStore(tmp_path, "00000000-0000-0000-0000-000000000003")
    human = HumanMessage("read")
    assistant = AssistantMessage(
        (TextContent("working"), ToolCall("call", "Read", {"path": "x"})),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    target.append(human)
    target.append(assistant)
    target.load_calls = 0

    state = ConversationState(_store(tmp_path, "4"))
    resumed = state.resume(target)

    assert isinstance(resumed[-1], ToolResultsMessage)
    assert resumed[-1].source_assistant_uuid == assistant.uuid
    assert resumed[-1].content[0].is_error is True
    assert target.load_calls == 1


def test_resume_empty_session_keeps_current_repository(tmp_path: Path) -> None:
    current = _store(tmp_path, "5")
    current.append(HumanMessage("current"))
    state = ConversationState(current)
    with pytest.raises(ValueError, match="no messages"):
        state.resume(_store(tmp_path, "6"))
    assert state.session_id == current.session_id


def test_failed_persistence_does_not_change_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "7")
    state = ConversationState(store)

    def fail(_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append_record", fail)
    with pytest.raises(OSError, match="disk full"):
        state.append(HumanMessage("not durable"))
    assert state.history == ()
    assert state.working_messages == ()


def test_compaction_updates_runtime_without_reload(tmp_path: Path) -> None:
    store = CountingSessionStore(tmp_path, "00000000-0000-0000-0000-000000000008")
    state = ConversationState(store)
    human = HumanMessage("hello")
    state.append(human)
    summary = ConversationSummaryMessage("summary", parent_uuid=human.uuid)
    boundary = CompactBoundary(human.uuid, summary.uuid, "manual", 5)
    replacement = ContentReplacement("call", "Read", 10, "short")

    state.commit_compaction(
        CompactionOutcome(
            replacements=(replacement,),
            summary=summary,
            boundary=boundary,
            usage=TokenUsage(),
        )
    )

    assert state.history == (human, summary)
    assert state.working_messages == (summary,)
    assert state.compact_boundaries == (boundary,)
    assert store.load_calls == 1


def test_partial_compaction_write_does_not_advance_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "10")
    state = ConversationState(store)
    human = HumanMessage("hello")
    state.append(human)
    summary = ConversationSummaryMessage("summary", parent_uuid=human.uuid)
    boundary = CompactBoundary(human.uuid, summary.uuid, "manual", 5)
    replacement = ContentReplacement("call", "Read", 10, "short")
    original_append_record = store._append_record

    def fail_summary(record: object) -> None:
        if (
            isinstance(record, dict)
            and record.get("type") == "conversation_summary_message"
        ):
            raise OSError("disk full")
        original_append_record(record)

    monkeypatch.setattr(store, "_append_record", fail_summary)
    with pytest.raises(OSError, match="disk full"):
        state.commit_compaction(
            CompactionOutcome(
                replacements=(replacement,),
                summary=summary,
                boundary=boundary,
                usage=TokenUsage(),
            )
        )

    assert state.history == (human,)
    assert state.working_messages == (human,)
    assert state.snapshot.content_replacements == ()
    assert state.compact_boundaries == ()


def test_external_transcript_append_is_visible_only_after_new_load(
    tmp_path: Path,
) -> None:
    session_id = "00000000-0000-0000-0000-000000000009"
    state = ConversationState(SessionStore(tmp_path, session_id))
    human = HumanMessage("local")
    state.append(human)

    external = AssistantMessage(
        (TextContent("external"),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    SessionStore(tmp_path, session_id).append(external)

    assert state.history == (human,)
    reloaded = ConversationState(SessionStore(tmp_path, session_id))
    assert reloaded.history == (human, external)
