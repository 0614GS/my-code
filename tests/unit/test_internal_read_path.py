from pathlib import Path

import pytest

from my_code.permissions.models import (
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
    ToolPermissionContext,
)
from my_code.tools.base import ToolContext, ToolInputError
from my_code.tools.builtin.read_file import ReadFileTool
from my_code.tools.paths import resolve_read_path


@pytest.mark.asyncio
async def test_read_accepts_only_controlled_project_temp_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "temp" / "project"
    workspace.mkdir()
    runtime.mkdir(parents=True)
    output = runtime / "session" / "tasks" / "id.output"
    output.parent.mkdir(parents=True)
    output.write_text("hello", encoding="utf-8")
    tool = ReadFileTool()

    result = await tool.execute(
        {"path": str(output)}, ToolContext(workspace, internal_read_root=runtime)
    )

    assert "hello" in result.content
    outside = tmp_path / "temp" / "other" / "id.output"
    outside.parent.mkdir(parents=True)
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ToolInputError, match="controlled runtime temp"):
        resolve_read_path(workspace, str(outside), internal_root=runtime)


def test_internal_read_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (runtime / "escape").symlink_to(outside)

    with pytest.raises(ToolInputError):
        resolve_read_path(
            workspace, str(runtime / "child" / ".." / "output"), internal_root=runtime
        )
    with pytest.raises(ToolInputError):
        resolve_read_path(workspace, str(runtime / "escape"), internal_root=runtime)


@pytest.mark.asyncio
async def test_explicit_read_deny_and_ask_precede_internal_allow(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    output = runtime / "task.output"
    output.write_text("data", encoding="utf-8")
    tool = ReadFileTool()

    for behavior in (PermissionBehavior.DENY, PermissionBehavior.ASK):
        result = await tool.check_permissions(
            {"path": str(output)},
            ToolPermissionContext(
                PermissionMode.DEFAULT,
                (PermissionRule("Read", behavior, str(output)),),
                workspace,
                internal_read_root=runtime,
            ),
        )
        assert result.behavior.value == behavior.value
