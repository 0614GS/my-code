from copy import deepcopy

import pytest

from my_code.conversation.attachments import RecentFileSnapshotAttachment
from my_code.sessions._codec import (
    TranscriptDecodeError,
    _attachment_from_json,
    _attachment_to_json,
)


def test_recent_file_snapshot_round_trip() -> None:
    complete = RecentFileSnapshotAttachment(
        "src/app.py", "one\ntwo\n", "a" * 64, 2, 1, 2, False
    )
    truncated = RecentFileSnapshotAttachment(
        "src/large.py", "one\n", "b" * 64, 2, 1, 1, True
    )
    empty = RecentFileSnapshotAttachment("empty.txt", "", "c" * 64, 0, 1, 0, False)

    for attachment in (complete, truncated, empty):
        assert _attachment_from_json(_attachment_to_json(attachment)) == attachment


@pytest.mark.parametrize(
    "change",
    ("unknown", "path", "text", "hash", "total", "start", "end", "truncated"),
)
def test_recent_file_snapshot_rejects_invalid_fields(change: str) -> None:
    raw = deepcopy(
        _attachment_to_json(
            RecentFileSnapshotAttachment("src/app.py", "one\n", "a" * 64, 2, 1, 1, True)
        )
    )
    if change == "unknown":
        raw["extra"] = True
    elif change == "path":
        raw["path"] = ""
    elif change == "text":
        raw["text"] = 42
    elif change == "hash":
        raw["sha256"] = "not-a-hash"
    elif change == "total":
        raw["total_lines"] = -1
    elif change == "start":
        raw["start_line"] = 2
    elif change == "end":
        raw["end_line"] = 3
    else:
        raw["truncated"] = False

    with pytest.raises(TranscriptDecodeError):
        _attachment_from_json(raw)


def test_recent_file_snapshot_requires_v9_codec() -> None:
    raw = _attachment_to_json(
        RecentFileSnapshotAttachment("src/app.py", "one\n", "a" * 64, 1, 1, 1, False)
    )

    with pytest.raises(TranscriptDecodeError, match="schema v9"):
        _attachment_from_json(raw, schema_version=8)
