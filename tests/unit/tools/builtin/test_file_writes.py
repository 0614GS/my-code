"""文件修改的完整读取、版本校验和原子发布测试。"""

from pathlib import Path

import pytest

from my_code.tools.base import ToolExecutionContext, ToolExecutionError
from my_code.tools.builtin.edit_file import EditFileTool
from my_code.tools.builtin.read_file import ReadFileTool
from my_code.tools.builtin.write_file import WriteFileTool


@pytest.mark.asyncio
async def test_existing_file_requires_complete_read_for_write_and_edit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    context = ToolExecutionContext(tmp_path)

    with pytest.raises(ToolExecutionError, match="entire current file"):
        await WriteFileTool().execute(
            {"path": "note.txt", "content": "replacement\n"}, context
        )
    await ReadFileTool().execute({"path": "note.txt", "limit": 1}, context)
    with pytest.raises(ToolExecutionError, match="entire current file"):
        await EditFileTool().execute(
            {"path": "note.txt", "old_string": "one", "new_string": "ONE"},
            context,
        )
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


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
