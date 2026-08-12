from pathlib import Path

import pytest

from nano_code.messages import ToolUseBlock
from nano_code.permissions import PermissionMode, PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.paths import resolve_workspace_path
from nano_code.tools.result_store import ToolResultStore


def build_executor(tmp_path: Path, mode: PermissionMode) -> ToolExecutor:
    return ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(mode),
        prompter=HeadlessPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )


def test_workspace_path_rejects_traversal_and_protected_writes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes the workspace"):
        resolve_workspace_path(tmp_path, "../outside", writable=True)
    with pytest.raises(ValueError, match="protected"):
        resolve_workspace_path(tmp_path, ".git/config", writable=True)
    with pytest.raises(ValueError, match="protected"):
        resolve_workspace_path(tmp_path, "claude-code/src/query.ts", writable=True)


@pytest.mark.asyncio
async def test_bypass_still_cannot_write_protected_path(tmp_path: Path) -> None:
    executor = build_executor(tmp_path, PermissionMode.BYPASS)
    result = await executor.execute(
        ToolUseBlock(
            id="write-protected",
            name="Write",
            input={"path": ".git/config", "content": "bad"},
        )
    )

    assert result.is_error is True
    assert "protected" in result.content


@pytest.mark.asyncio
async def test_unknown_tool_produces_matching_error_result(tmp_path: Path) -> None:
    executor = build_executor(tmp_path, PermissionMode.DEFAULT)
    result = await executor.execute(
        ToolUseBlock(id="unknown-1", name="Missing", input={})
    )

    assert result.tool_use_id == "unknown-1"
    assert result.is_error is True
    assert "Unknown tool" in result.content
