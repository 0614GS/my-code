import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nano_code.agent import CompactBoundary, ContentReplacement
from nano_code.messages import (
    ChatMessage,
    SystemContextBlock,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
)
from nano_code.presentation import ToolResultPresentation
from nano_code.sessions import SessionCatalog, SessionStore

_SESSION_ID = "12345678-1234-1234-1234-123456789abc"


def test_append_is_idempotent_and_round_trips(tmp_path: Path) -> None:
    project_state_dir = tmp_path / "projects" / "-workspace"
    store = SessionStore(project_state_dir, _SESSION_ID)
    first = ChatMessage(role="user", origin="human", content=(TextBlock("hi"),))
    second = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("hello"),),
        parent_uuid=first.uuid,
    )

    store.append(first)
    store.append(first)
    store.append(second)

    assert store.load() == (first, second)
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 2
    assert store.path == project_state_dir / f"{_SESSION_ID}.jsonl"
    assert store.session_dir == project_state_dir / _SESSION_ID
    assert not store.session_dir.exists()


def test_tool_result_presentation_round_trips_with_transcript(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    presentation = ToolResultPresentation(
        summary="Read 12 lines from src/app.py",
        detail="first line preview",
        truncated=True,
    )
    message = ChatMessage(
        role="user",
        origin="tool",
        content=(
            ToolResultBlock(
                "tool-1",
                "full model-visible result",
                presentation=presentation,
            ),
        ),
    )

    store.append(message)

    assert store.load() == (message,)
    loaded = store.load()[0].content[0]
    assert isinstance(loaded, ToolResultBlock)
    assert loaded.presentation == presentation


def test_assistant_usage_round_trips_with_transcript(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    message = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("answer"),),
        usage=TokenUsage(
            input_tokens=120,
            output_tokens=8,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=20,
        ),
    )

    store.append(message)

    assert store.load() == (message,)


def test_system_context_round_trips_without_persisting_rendered_xml(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    message = ChatMessage(
        role="user",
        origin="system",
        content=(
            SystemContextBlock(
                kind="conversation_summary",
                content="Continue from the verified state.",
            ),
        ),
    )

    store.append(message)

    assert store.load() == (message,)
    transcript = store.path.read_text(encoding="utf-8")
    assert '"type": "system_context"' in transcript
    assert "<conversation-summary>" not in transcript


def test_content_replacement_is_append_only_and_round_trips(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    message = ChatMessage(role="user", origin="human", content=(TextBlock("hi"),))
    replacement = ContentReplacement.for_tool_result(
        tool_use_id="tool-1",
        tool_name="Read",
        original_chars=5000,
    )
    store.append(message)

    store.append_content_replacement(replacement)
    store.append_content_replacement(replacement)

    assert store.load() == (message,)
    assert store.load_content_replacements() == (replacement,)
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 2


def test_incomplete_compact_boundary_is_ignored_on_recovery(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    message = ChatMessage(role="user", origin="human", content=(TextBlock("hi"),))
    store.append(message)
    store.append_compact_boundary(
        CompactBoundary(
            parent_uuid=message.uuid,
            summary_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            trigger="auto",
            pre_compact_chars=2,
        )
    )

    recovered = SessionStore(tmp_path, _SESSION_ID)

    assert recovered.load() == (message,)
    assert recovered.load_compact_boundaries() == ()
    assert recovered.load_working_set() == (message,)


def test_old_tool_result_without_presentation_remains_loadable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    store.project_state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "message",
        "version": 1,
        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "parent_uuid": None,
        "timestamp": "2026-08-13T00:00:00+00:00",
        "role": "user",
        "origin": "tool",
        "source_message_uuid": None,
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "legacy-call",
                "content": "legacy result",
                "is_error": False,
            }
        ],
    }
    store.path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    loaded = store.load()[0].content[0]

    assert isinstance(loaded, ToolResultBlock)
    assert loaded.presentation is None


def test_rejects_missing_parent_on_append(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    orphan = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("hello"),),
        parent_uuid="missing",
    )

    with pytest.raises(ValueError, match="Unknown parent UUID"):
        store.append(orphan)


def test_rejects_corrupt_transcript(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    store.project_state_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"type": "unknown"}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid transcript line"):
        store.load()


def test_rejects_non_uuid_session_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a UUID"):
        SessionStore(tmp_path, "../session")


def test_load_returns_only_active_parent_chain(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _SESSION_ID)
    root = ChatMessage(role="user", origin="human", content=(TextBlock("root"),))
    abandoned = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("abandoned"),),
        parent_uuid=root.uuid,
    )
    active = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("active"),),
        parent_uuid=root.uuid,
    )

    store.append(root)
    store.append(abandoned)
    store.append(active)

    assert store.load() == (root, active)


def test_catalog_lists_valid_sessions_by_modified_time_and_first_prompt(
    tmp_path: Path,
) -> None:
    older_id = "11111111-1111-1111-1111-111111111111"
    newer_id = "22222222-2222-2222-2222-222222222222"
    excluded_id = "33333333-3333-3333-3333-333333333333"
    for session_id, prompt in (
        (older_id, "older prompt\nwith spacing"),
        (newer_id, "newer prompt"),
        (excluded_id, "current prompt"),
    ):
        SessionStore(tmp_path, session_id).append(
            ChatMessage(
                role="user",
                origin="human",
                content=(TextBlock(prompt),),
            )
        )
    os.utime(tmp_path / f"{older_id}.jsonl", (100, 100))
    os.utime(tmp_path / f"{newer_id}.jsonl", (200, 200))
    os.utime(tmp_path / f"{excluded_id}.jsonl", (300, 300))
    (tmp_path / "not-a-session.jsonl").write_text("{}\n", encoding="utf-8")

    sessions = SessionCatalog(tmp_path).list(exclude_session_id=excluded_id)

    assert [session.session_id for session in sessions] == [newer_id, older_id]
    assert sessions[1].title == "older prompt with spacing"
    assert sessions[0].updated_at == datetime.fromtimestamp(200, UTC)
