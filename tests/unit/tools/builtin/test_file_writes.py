"""文件修改的观察门槛、版本校验和原子发布测试。"""

import asyncio
from pathlib import Path

import pytest

from my_code.foundation.json import JsonObject
from my_code.tools.base import ToolExecutionContext, ToolExecutionError
from my_code.tools.builtin.edit_file import EditFileTool
from my_code.tools.builtin.read_file import ReadFileTool
from my_code.tools.builtin.write_file import WriteFileTool
from my_code.tools.file_state import FileReadTracker


@pytest.mark.asyncio
async def test_edit_accepts_partial_read_but_write_requires_complete_read(
    tmp_path: Path,
) -> None:
    edit_path = tmp_path / "edit.txt"
    write_path = tmp_path / "write.txt"
    edit_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    write_path.write_text("one\ntwo\n", encoding="utf-8")
    context = ToolExecutionContext(tmp_path)

    with pytest.raises(ToolExecutionError, match="Read the current file"):
        await EditFileTool().execute(
            {"path": "edit.txt", "old_string": "three", "new_string": "THREE"},
            context,
        )
    await ReadFileTool().execute({"path": "edit.txt", "limit": 1}, context)
    await EditFileTool().execute(
        {"path": "edit.txt", "old_string": "three", "new_string": "THREE"},
        context,
    )

    await ReadFileTool().execute({"path": "write.txt", "limit": 1}, context)
    with pytest.raises(ToolExecutionError, match="entire current file"):
        await WriteFileTool().execute(
            {"path": "write.txt", "content": "replacement\n"}, context
        )

    assert edit_path.read_text(encoding="utf-8") == "one\ntwo\nTHREE\n"
    assert write_path.read_text(encoding="utf-8") == "one\ntwo\n"


@pytest.mark.asyncio
async def test_paginated_read_authorizes_edit_and_write_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    context = ToolExecutionContext(tmp_path)
    read = ReadFileTool()

    await read.execute({"path": "note.txt", "limit": 1}, context)
    await read.execute({"path": "note.txt", "offset": 2, "limit": 1}, context)
    await EditFileTool().execute(
        {"path": "note.txt", "old_string": "one", "new_string": "ONE"},
        context,
    )
    await WriteFileTool().execute({"path": "note.txt", "content": "final\n"}, context)

    assert path.read_text(encoding="utf-8") == "final\n"
    recent_paths = tuple(
        item.path for item in context.recent_files.recent("__anonymous__")
    )
    assert recent_paths == (path,)


@pytest.mark.asyncio
async def test_external_change_after_read_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original\n", encoding="utf-8")
    context = ToolExecutionContext(tmp_path)
    await ReadFileTool().execute({"path": "note.txt"}, context)
    path.write_text("external\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="changed since Read"):
        await WriteFileTool().execute(
            {"path": "note.txt", "content": "model\n"}, context
        )

    assert path.read_text(encoding="utf-8") == "external\n"


@pytest.mark.asyncio
async def test_edit_rejects_external_change_after_partial_read(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    context = ToolExecutionContext(tmp_path)
    await ReadFileTool().execute({"path": "note.txt", "limit": 1}, context)
    path.write_text("one\nexternal\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="changed since Read"):
        await EditFileTool().execute(
            {"path": "note.txt", "old_string": "one", "new_string": "ONE"},
            context,
        )

    assert path.read_text(encoding="utf-8") == "one\nexternal\n"


@pytest.mark.asyncio
async def test_visible_truncated_line_authorizes_edit_but_empty_range_does_not(
    tmp_path: Path,
) -> None:
    long_path = tmp_path / "long.txt"
    skipped_path = tmp_path / "skipped.txt"
    long_path.write_text("target" + "x" * 30_000, encoding="utf-8")
    skipped_path.write_text("target\n", encoding="utf-8")
    context = ToolExecutionContext(tmp_path)

    truncated = await ReadFileTool().execute({"path": "long.txt"}, context)
    assert truncated.metadata["truncated_by"] == "line_chars"
    await EditFileTool().execute(
        {"path": "long.txt", "old_string": "target", "new_string": "changed"},
        context,
    )

    await ReadFileTool().execute({"path": "skipped.txt", "offset": 2}, context)
    with pytest.raises(ToolExecutionError, match="Read the current file"):
        await EditFileTool().execute(
            {
                "path": "skipped.txt",
                "old_string": "target",
                "new_string": "changed",
            },
            context,
        )


@pytest.mark.asyncio
async def test_partial_read_authorization_is_isolated_by_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    reads = FileReadTracker()
    first = ToolExecutionContext(tmp_path, session_id="first", file_reads=reads)
    second = ToolExecutionContext(tmp_path, session_id="second", file_reads=reads)
    tool_input: JsonObject = {
        "path": "note.txt",
        "old_string": "two",
        "new_string": "TWO",
    }

    await ReadFileTool().execute({"path": "note.txt", "limit": 1}, first)
    with pytest.raises(ToolExecutionError, match="Read the current file"):
        await EditFileTool().execute(tool_input, second)
    await EditFileTool().execute(tool_input, first)

    assert path.read_text(encoding="utf-8") == "one\nTWO\n"


@pytest.mark.asyncio
async def test_cancelled_edit_waiting_for_lease_preserves_file_and_observation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    context = ToolExecutionContext(tmp_path)
    await ReadFileTool().execute({"path": "note.txt", "limit": 1}, context)
    tool = EditFileTool()
    tool_input: JsonObject = {
        "path": "note.txt",
        "old_string": "two",
        "new_string": "TWO",
    }

    async with context.workspace.coordinator.path_lease(path, write=True):
        task = asyncio.create_task(tool.execute(tool_input, context))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert path.read_text(encoding="utf-8") == "one\ntwo\n"
    await tool.execute(tool_input, context)
    assert path.read_text(encoding="utf-8") == "one\nTWO\n"


@pytest.mark.asyncio
async def test_new_file_is_created_atomically_without_read(tmp_path: Path) -> None:
    context = ToolExecutionContext(tmp_path)

    await WriteFileTool().execute(
        {"path": "nested/new.txt", "content": "created\n"}, context
    )

    assert (tmp_path / "nested/new.txt").read_text(encoding="utf-8") == "created\n"
    assert list((tmp_path / "nested").glob(".new.txt.*.tmp")) == []
    assert context.recent_files.recent("__anonymous__")[0].path == (
        tmp_path / "nested/new.txt"
    )
