import json
from pathlib import Path

import pytest

import my_code.sessions._store as store_module
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
from my_code.sessions._codec import (
    decode_entry,
    entry_from_json,
    entry_to_json,
    message_to_record,
)
from my_code.sessions._store import SessionStore
from my_code.sessions.catalog import SessionCatalog
from my_code.tools.presentation import ToolResultPresentation

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
    results = ToolResultBatch(
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
        "tool_result_batch",
        "session_metadata",
        "conversation_summary_message",
        "session_metadata",
    ]
    assert all(entry["schema_version"] == 5 for entry in entries)
    assert entries[0]["max_steps"] is None
    assert "max_turns" not in entries[0]
    assert all("role" not in entry and "origin" not in entry for entry in entries)


def test_codec_round_trip_each_message_variant() -> None:
    for message in _chain():
        record = message_to_record(message)
        assert entry_from_json(entry_to_json(record)) == record


def test_legacy_tool_results_message_decodes_as_tool_result_batch() -> None:
    _, _, batch, _ = _chain()
    document = entry_to_json(message_to_record(batch))
    document["type"] = "tool_results_message"
    document["source_assistant_uuid"] = document.pop("source_assistant_id")

    restored = decode_entry(document)

    assert restored == batch


def test_reasoning_assistant_content_round_trips_v5(tmp_path: Path) -> None:
    store = _store(tmp_path)
    human = HumanMessage("hello")
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
            ToolCall("call", "Read", {"path": "x"}),
        ),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    store.append(human)
    store.append_message(
        assistant,
        replay_records=(
            ProviderReplayRecord(assistant.uuid, replay_content_id(0), continuation),
        ),
    )

    loaded = store.load()
    restored = loaded.history[1]
    assert isinstance(restored, AssistantMessage)
    assert not any(hasattr(block, "continuation") for block in restored.content)
    assert len(loaded.replay_records) == 1
    assert loaded.replay_records[0].state == continuation
    document = [json.loads(line) for line in store.path.read_text().splitlines()]
    assistant_record = next(
        entry for entry in document if entry["type"] == "assistant_message"
    )
    assert assistant_record["content"][0]["type"] == "reasoning"
    assert "continuation" not in assistant_record["content"][0]
    replay_record = next(
        entry for entry in document if entry["type"] == "provider_replay"
    )
    assert replay_record["entry_id"] == assistant.uuid
    assert replay_record["content_id"] == "content:0"


def test_atomic_append_failure_preserves_previous_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    first = HumanMessage("first")
    store.append(first)
    before = store.path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.append(HumanMessage("second", parent_uuid=first.uuid))

    assert store.path.read_bytes() == before
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_legacy_embedded_continuation_is_projected_to_replay_sidecar(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    human = HumanMessage("hello")
    assistant = AssistantMessage(
        (
            ReasoningContent("thinking", ReasoningPresentation("hidden")),
            TextContent("answer"),
        ),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    continuation = {
        "binding": {
            "protocol": "anthropic-messages",
            "provider_id": "anthropic",
            "model": "claude-test",
            "base_url": None,
        },
        "replay_scope": "working_context",
        "payload": {"type": "thinking", "thinking": "", "signature": "signed"},
    }
    store.append(human)
    store.append(assistant)
    documents = [json.loads(line) for line in store.path.read_text().splitlines()]
    assistant_document = next(
        item for item in documents if item["type"] == "assistant_message"
    )
    assistant_document["content"][0]["continuation"] = continuation
    store.path.write_text(
        "".join(json.dumps(item) + "\n" for item in documents), encoding="utf-8"
    )

    loaded = SessionStore(tmp_path, SESSION_ID).load()

    assert not hasattr(loaded.history[1].content[0], "continuation")  # type: ignore[union-attr]
    assert loaded.replay_records[0].entry_id == assistant.uuid
    assert loaded.replay_records[0].state.payload["signature"] == "signed"


@pytest.mark.parametrize(
    "field,value", [("entry_id", SESSION_ID), ("content_id", "content:9")]
)
def test_restore_rejects_invalid_replay_association(
    tmp_path: Path, field: str, value: str
) -> None:
    store = _store(tmp_path)
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
    store.append(human)
    store.append_message(assistant, replay_records=(replay,))
    documents = [json.loads(line) for line in store.path.read_text().splitlines()]
    replay_document = next(
        item for item in documents if item["type"] == "provider_replay"
    )
    replay_document[field] = value
    store.path.write_text(
        "".join(json.dumps(item) + "\n" for item in documents), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="Provider replay references"):
        SessionStore(tmp_path, SESSION_ID).load()


def test_tool_presentation_is_a_session_add_on_not_a_conversation_fact(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    human, assistant, results, _ = _chain()
    presentation = ToolResultPresentation("Read x", "historical detail")
    store.append(human)
    store.append(assistant)
    store.append_message(results, (("call", presentation),))

    loaded = store.load()
    restored_result = loaded.history[-1]
    assert isinstance(restored_result, ToolResultBatch)
    assert not hasattr(restored_result.content[0], "presentation")
    assert dict(loaded.tool_presentations) == {"call": presentation}
    document = [json.loads(line) for line in store.path.read_text().splitlines()]
    presentation_record = next(
        entry for entry in document if entry["type"] == "tool_presentation"
    )
    result_record = next(
        entry for entry in document if entry["type"] == "tool_result_batch"
    )
    assert presentation_record["tool_use_id"] == "call"
    assert "presentation" not in result_record["content"][0]


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


@pytest.mark.parametrize(
    ("document", "version"),
    [
        ({"type": "message", "version": 1}, 1),
        ({"type": "session_started", "schema_version": 2}, 2),
        ({"type": "session_started", "schema_version": 3}, 3),
        ({"type": "session_started", "schema_version": 4}, 4),
    ],
)
def test_catalog_skips_legacy_transcript(
    tmp_path: Path, document: object, version: int
) -> None:
    legacy = tmp_path / f"{SESSION_ID}.jsonl"
    legacy.write_text(json.dumps(document) + "\n")
    assert SessionCatalog(tmp_path).list() == ()
    with pytest.raises(ValueError, match=f"schema v{version} is incompatible"):
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
