import json
from pathlib import Path

import pytest

from nano_code.agent import CompactBoundary, ContentReplacement
from nano_code.messages import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.sessions import SessionCatalog, SessionStore
from nano_code.sessions.codec import entry_from_json, entry_to_json, message_to_record

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path, SESSION_ID)


def _chain():
    human = HumanMessage("hello")
    assistant = AssistantMessage(
        (TextContent("answer"), ToolCall("call", "Read", {"path": "x"})),
        TokenUsage(10, 2),
        parent_uuid=human.uuid,
    )
    results = ToolResultsMessage(
        (ToolResult("call", "value"),), assistant.uuid, parent_uuid=assistant.uuid
    )
    summary = ConversationSummaryMessage("state", parent_uuid=results.uuid)
    return human, assistant, results, summary


def test_four_message_records_round_trip_new_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    messages = _chain()
    for message in messages:
        store.append(message)

    assert store.load().history == messages
    entries = [json.loads(line) for line in store.path.read_text().splitlines()]
    assert [entry["type"] for entry in entries] == [
        "session_started",
        "human_message",
        "session_metadata",
        "assistant_message",
        "session_metadata",
        "tool_results_message",
        "session_metadata",
        "conversation_summary_message",
        "session_metadata",
    ]
    assert all(entry["schema_version"] == 2 for entry in entries)
    assert all("role" not in entry and "origin" not in entry for entry in entries)


def test_codec_round_trip_each_message_variant() -> None:
    for message in _chain():
        record = message_to_record(message)
        assert entry_from_json(entry_to_json(record)) == record


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "message", "version": 1},
        {"type": "human_message", "schema_version": 2},
        {"type": "unknown", "schema_version": 1},
    ],
)
def test_old_unknown_and_wrong_version_entries_are_rejected(entry: object) -> None:
    with pytest.raises(ValueError):
        entry_from_json(entry)


def test_record_rejects_redundant_role_and_origin_fields() -> None:
    record = entry_to_json(message_to_record(HumanMessage("hello")))
    record["role"] = "user"
    record["origin"] = "human"
    with pytest.raises(ValueError, match="unexpected"):
        entry_from_json(record)


def test_parent_chain_idempotency_and_active_branch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = HumanMessage("root")
    first = AssistantMessage(
        (TextContent("first"),), TokenUsage(), parent_uuid=root.uuid
    )
    branch = AssistantMessage(
        (TextContent("branch"),), TokenUsage(), parent_uuid=root.uuid
    )
    assert store.append(root) is True
    assert store.append(first) is True
    assert store.append(branch) is True
    assert store.append(branch) is False
    assert store.load().history == (root, branch)
    with pytest.raises(ValueError, match="Unknown parent"):
        store.append(HumanMessage("bad", parent_uuid="missing"))


def test_structured_records_and_compact_boundary_are_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    human = HumanMessage("hello")
    summary = ConversationSummaryMessage("state", parent_uuid=human.uuid)
    replacement = ContentReplacement("call", "Read", 100, "short")
    boundary = CompactBoundary(human.uuid, summary.uuid, "manual", 100)
    store.append(human)
    store.append_content_replacement(replacement)
    store.append_compact_boundary(boundary)
    before_summary = store.load()
    assert before_summary.compact_boundaries == ()
    store.append(summary)
    after_summary = store.load()
    assert after_summary.content_replacements == (replacement,)
    assert after_summary.compact_boundaries == (boundary,)
    assert after_summary.working_set == (summary,)


def test_catalog_skips_legacy_transcript(tmp_path: Path) -> None:
    legacy = tmp_path / f"{SESSION_ID}.jsonl"
    legacy.write_text(json.dumps({"type": "message", "version": 1}) + "\n")
    assert SessionCatalog(tmp_path).list() == ()
    with pytest.raises(ValueError, match="incompatible"):
        _store(tmp_path).load()


def test_failed_append_does_not_update_idempotency_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    message = HumanMessage("hello")

    def fail(_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append_records", fail)
    with pytest.raises(OSError, match="disk full"):
        store.append(message)
    assert message.uuid not in (store._known_ids or set())


def test_same_uuid_with_different_content_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    message = HumanMessage("first")
    store.append(message)

    conflicting = HumanMessage(
        "different",
        uuid=message.uuid,
        timestamp=message.timestamp,
    )
    with pytest.raises(ValueError, match="Conflicting message UUID"):
        store.append(conflicting)
