"""Structured file diff generation and file-tool integration."""

from pathlib import Path

import pytest

from my_code.conversation.models import ToolCall
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.builtin import builtin_tools
from my_code.tools.builtin.file_diff import (
    MAX_DIFF_BYTES,
    MAX_DIFF_RECORDS,
    build_file_diff,
    record_count,
)
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.executor import ToolExecutor
from my_code.workspace.local import Workspace


def _executor(path: Path) -> ToolExecutor:
    return ToolExecutor(
        ToolCatalogSnapshot.from_tools(builtin_tools()),
        PermissionPolicy(PermissionMode.ACCEPT_EDITS),
        HeadlessPrompter(),
        Workspace(path),
    )


def test_diff_tracks_multiple_hunks_and_complete_statistics() -> None:
    before = "".join(f"line {index}\n" for index in range(30))
    after = before.replace("line 2\n", "line two\n").replace(
        "line 25\n", "line twenty-five\nextra\n"
    )

    diff = build_file_diff("sample.py", before, after, created=False)

    assert (diff.additions, diff.deletions) == (3, 2)
    assert len(diff.hunks) == 2
    assert [line.kind for line in diff.hunks[0].lines].count("context") <= 6
    assert diff.old_ends_with_newline
    assert diff.new_ends_with_newline


def test_diff_detects_moves_as_delete_and_add_and_eof_newline_changes() -> None:
    moved = build_file_diff(
        "x.txt", "one\ntwo\nthree\n", "two\nthree\none\n", created=False
    )
    newline = build_file_diff("x.txt", "same\n", "same", created=False)

    assert moved.additions > 0
    assert moved.deletions > 0
    assert (newline.additions, newline.deletions) == (1, 1)
    assert newline.old_ends_with_newline
    assert not newline.new_ends_with_newline


@pytest.mark.parametrize(
    ("before", "after", "created", "counts"),
    [
        ("", "one\ntwo\n", True, (2, 0)),
        ("one\ntwo\n", "", False, (0, 2)),
        ("same\n", "same\n", False, (0, 0)),
    ],
)
def test_diff_handles_create_clear_and_no_change(
    before: str, after: str, created: bool, counts: tuple[int, int]
) -> None:
    diff = build_file_diff("x.txt", before, after, created=created)

    assert (diff.additions, diff.deletions) == counts
    assert diff.operation == ("created" if created else "updated")


def test_diff_truncation_keeps_full_statistics_with_a_hard_record_limit() -> None:
    before = "".join(f"old {index}\n" for index in range(400))
    after = "".join(f"new {index}\n" for index in range(400))

    diff = build_file_diff("large.txt", before, after, created=False)

    assert (diff.additions, diff.deletions) == (400, 400)
    assert record_count(diff.hunks) <= MAX_DIFF_RECORDS
    assert diff.omitted_lines > 0
    visible = [line.text for hunk in diff.hunks for line in hunk.lines]
    assert "old 0" in visible
    assert "new 399" in visible


def test_too_many_hunks_keeps_the_first_and_last_changes() -> None:
    before_lines = [f"line {index}\n" for index in range(1_500)]
    after_lines = before_lines.copy()
    changed_indexes = list(range(0, 1_500, 10))
    for index in changed_indexes:
        after_lines[index] = f"changed {index}\n"

    diff = build_file_diff(
        "many.txt", "".join(before_lines), "".join(after_lines), created=False
    )

    assert record_count(diff.hunks) <= MAX_DIFF_RECORDS
    assert diff.omitted_lines > 0
    visible = {line.text for hunk in diff.hunks for line in hunk.lines}
    assert "line 0" in visible
    assert "changed 0" in visible
    assert "line 1490" in visible
    assert "changed 1490" in visible


def test_oversized_file_skips_expensive_diff() -> None:
    diff = build_file_diff("huge.txt", "x" * (MAX_DIFF_BYTES + 1), "y", created=False)

    assert diff.hunks == ()
    assert diff.omitted_reason == "diff omitted because the file is too large"


@pytest.mark.asyncio
async def test_write_and_edit_attach_diff_only_to_successful_results(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    created = await executor.execute(
        ToolCall("write", "Write", {"path": "x.py", "content": "value = 1\n"})
    )
    edited = await executor.execute(
        ToolCall(
            "edit",
            "Edit",
            {
                "path": "x.py",
                "old_string": "1",
                "new_string": "2",
                "replace_all": False,
            },
        )
    )
    failed = await executor.execute(
        ToolCall(
            "failed",
            "Edit",
            {"path": "x.py", "old_string": "missing", "new_string": "no"},
        )
    )

    assert created.result.presentation.file_diff is not None
    assert created.result.presentation.file_diff.operation == "created"
    assert edited.result.presentation.file_diff is not None
    assert edited.result.presentation.file_diff.operation == "updated"
    assert failed.result.is_error
    assert failed.result.presentation.file_diff is None


@pytest.mark.asyncio
async def test_replace_all_produces_separate_hunks_with_complete_counts(
    tmp_path: Path,
) -> None:
    content = "target\n" + "".join(f"middle {index}\n" for index in range(20))
    (tmp_path / "x.txt").write_text(content + "target\n", encoding="utf-8")
    outcome = await _executor(tmp_path).execute(
        ToolCall(
            "edit-all",
            "Edit",
            {
                "path": "x.txt",
                "old_string": "target",
                "new_string": "changed",
                "replace_all": True,
            },
        )
    )

    diff = outcome.result.presentation.file_diff
    assert diff is not None
    assert (diff.additions, diff.deletions) == (2, 2)
    assert len(diff.hunks) == 2
