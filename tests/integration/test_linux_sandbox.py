from pathlib import Path

import pytest

from my_code.tools.base import ToolExecutionContext
from my_code.tools.builtin.bash.process import execute_bash
from my_code.workspace.launcher import resolve_command_launcher


@pytest.mark.asyncio
async def test_linux_bubblewrap_workspace_and_metadata_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = resolve_command_launcher(workspace)
    if not launcher.status.sandboxed:
        pytest.skip(launcher.status.fallback_reason or "bubblewrap unavailable")
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")
    context = ToolExecutionContext(workspace, command_launcher=launcher)

    created = await execute_bash(
        'printf changed > visible.txt; printf temp > "$TMPDIR/file"',
        context,
        10,
    )
    denied = await execute_bash(
        f"printf changed > {outside}",
        context,
        10,
    )
    protected = await execute_bash(
        "printf changed > .git/config 2>/dev/null || true; "
        "printf changed > .my-code/settings.json 2>/dev/null || true; "
        "test ! -e .git/config; test ! -e .my-code/settings.json",
        context,
        10,
    )

    assert created.is_error is False
    assert (workspace / "visible.txt").read_text(encoding="utf-8") == "changed"
    assert denied.is_error is True
    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert protected.is_error is False
    assert not (workspace / ".git").exists()
    assert not (workspace / ".my-code").exists()


@pytest.mark.asyncio
async def test_linux_bubblewrap_regular_tmp_is_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = resolve_command_launcher(workspace)
    if not launcher.status.sandboxed:
        pytest.skip(launcher.status.fallback_reason or "bubblewrap unavailable")
    context = ToolExecutionContext(workspace, command_launcher=launcher)
    target = tmp_path / "ordinary-tmp-must-not-write"

    output = await execute_bash(f"printf unsafe > {target}", context, 10)

    assert output.is_error is True
    assert not target.exists()
