import json
from pathlib import Path

import pytest

from nano_code.agent import (
    CompactBoundary,
    CompactionOutcome,
    ContentReplacement,
    ConversationState,
)
from nano_code.messages import (
    SystemContextBlock,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)
from nano_code.sessions import SessionStore

_SESSION_ID = "12345678-1234-1234-1234-123456789abc"
_OTHER_SESSION_ID = "87654321-4321-4321-4321-cba987654321"


def test_snapshot_and_compaction_commit_keep_persistence_order(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    user = TranscriptMessage(
        role="user", origin="human", content=(TextBlock("inspect"),)
    )
    assistant = TranscriptMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("read-1", "Read", {"path": "a.txt"}),),
        parent_uuid=user.uuid,
    )
    result = TranscriptMessage(
        role="user",
        origin="tool",
        content=(ToolResultBlock("read-1", "content"),),
        parent_uuid=assistant.uuid,
        source_message_uuid=assistant.uuid,
    )
    for message in (user, assistant, result):
        store.append(message)

    state = ConversationState(store)
    replacement = ContentReplacement.for_tool_result(
        tool_use_id="read-1",
        tool_name="Read",
        original_chars=100,
    )
    summary = TranscriptMessage(
        role="user",
        origin="system",
        content=(
            SystemContextBlock(
                kind="conversation_summary",
                content="Continue from the inspected file.",
            ),
        ),
        parent_uuid=result.uuid,
    )
    boundary = CompactBoundary(
        parent_uuid=result.uuid,
        summary_uuid=summary.uuid,
        trigger="manual",
        pre_compact_chars=100,
    )

    state.commit_compaction(
        CompactionOutcome(
            replacements=(replacement,),
            summary=summary,
            boundary=boundary,
            usage=TokenUsage(),
        )
    )

    records = [
        json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record.get("type", "message") for record in records[-3:]] == [
        "content_replacement",
        "compact_boundary",
        "message",
    ]
    assert state.working_messages == (summary,)
    snapshot = store.snapshot()
    assert snapshot.history[-1] == summary
    assert snapshot.working_set == (summary,)
    assert snapshot.content_replacements == (replacement,)
    assert snapshot.compact_boundaries == (boundary,)


def test_compaction_write_failure_does_not_change_working_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    user = TranscriptMessage(role="user", origin="human", content=(TextBlock("keep"),))
    store.append(user)
    state = ConversationState(store)
    summary = TranscriptMessage(
        role="user",
        origin="system",
        content=(
            SystemContextBlock(
                kind="conversation_summary",
                content="summary",
            ),
        ),
        parent_uuid=user.uuid,
    )
    boundary = CompactBoundary(
        parent_uuid=user.uuid,
        summary_uuid=summary.uuid,
        trigger="auto",
        pre_compact_chars=4,
    )
    original_append = store.append

    def fail_summary(message: TranscriptMessage) -> None:
        if message.uuid == summary.uuid:
            raise OSError("disk full")
        original_append(message)

    monkeypatch.setattr(store, "append", fail_summary)

    with pytest.raises(OSError, match="disk full"):
        state.commit_compaction(CompactionOutcome((), summary, boundary, TokenUsage()))

    assert state.working_messages == (user,)
    assert state.compact_count == 0
    assert store.load_compact_boundaries() == ()


def test_resume_repairs_before_switching_repository(tmp_path: Path) -> None:
    current_store = SessionStore(tmp_path, _SESSION_ID)
    current = TranscriptMessage(
        role="user", origin="human", content=(TextBlock("current"),)
    )
    current_store.append(current)
    state = ConversationState(current_store)

    target_store = SessionStore(tmp_path, _OTHER_SESSION_ID)
    target_user = TranscriptMessage(
        role="user", origin="human", content=(TextBlock("target"),)
    )
    target_assistant = TranscriptMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("unfinished", "Read", {"path": "x"}),),
        parent_uuid=target_user.uuid,
    )
    target_store.append(target_user)
    target_store.append(target_assistant)

    loaded = state.resume(target_store)

    assert state.session_id == _OTHER_SESSION_ID
    assert loaded[-1].origin == "tool"
    assert isinstance(loaded[-1].content[0], ToolResultBlock)
    assert state.working_messages[-1].origin == "tool"
