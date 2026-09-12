from copy import deepcopy

import pytest

from my_code.conversation.attachments import TodoSnapshotAttachment, TodoSnapshotEntry
from my_code.sessions._codec import (
    TranscriptDecodeError,
    _attachment_from_json,
    _attachment_to_json,
)


def test_todo_snapshot_round_trip() -> None:
    snapshot = TodoSnapshotAttachment(
        "write",
        (
            TodoSnapshotEntry("task", "in_progress", "doing task"),
            TodoSnapshotEntry("done", "completed", "finishing"),
        ),
    )
    assert _attachment_from_json(_attachment_to_json(snapshot)) == snapshot
    empty = TodoSnapshotAttachment("clear", ())
    assert _attachment_from_json(_attachment_to_json(empty)) == empty


@pytest.mark.parametrize(
    "change", ["unknown", "unknown_item", "status", "content", "source", "items"]
)
def test_todo_snapshot_rejects_invalid_fields(change: str) -> None:
    raw = deepcopy(
        _attachment_to_json(
            TodoSnapshotAttachment(
                "write", (TodoSnapshotEntry("task", "pending", "doing"),)
            )
        )
    )
    if change == "unknown":
        raw["extra"] = True
    elif change == "source":
        raw["source_write_id"] = ""
    elif change == "items":
        raw["todos"] = {}
    else:
        items = raw["todos"]
        assert isinstance(items, list)
        item = items[0]
        assert isinstance(item, dict)
        item[
            {"unknown_item": "extra", "status": "status", "content": "content"}[change]
        ] = "invalid" if change != "content" else 42
    with pytest.raises(TranscriptDecodeError):
        _attachment_from_json(raw)
