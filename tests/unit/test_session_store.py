import json
from pathlib import Path

import pytest

from nano_code.messages import ChatMessage, TextBlock
from nano_code.sessions import SessionStore

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
