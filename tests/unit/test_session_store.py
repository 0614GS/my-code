import json
from pathlib import Path

import pytest

from nano_code.messages import ChatMessage, TextBlock
from nano_code.sessions import SessionStore


def test_append_is_idempotent_and_round_trips(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, "session")
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


def test_rejects_missing_parent_on_append(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, "session")
    orphan = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("hello"),),
        parent_uuid="missing",
    )

    with pytest.raises(ValueError, match="Unknown parent UUID"):
        store.append(orphan)


def test_rejects_corrupt_transcript(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, "session")
    store.session_dir.mkdir(parents=True)
    store.path.write_text(json.dumps({"type": "unknown"}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid transcript line"):
        store.load()
