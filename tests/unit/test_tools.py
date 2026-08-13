from pathlib import Path

import pytest

from nano_code.messages import JsonObject, ToolUseBlock
from nano_code.permissions import (
    PermissionConfirmation,
    PermissionMode,
    PermissionPolicy,
    ToolPermissionContext,
    ToolPermissionResult,
)
from nano_code.permissions.models import PermissionDecision
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.tools import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolRisk,
)
from nano_code.tools.base import ToolOutput
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


class FeedbackPrompter:
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        return PermissionConfirmation(False, "Only explain the proposed change.")


class FailingPrompter:
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        raise AssertionError("read-only Bash must not request user confirmation")


class NormalizingTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="Normalize",
            description="Test approved-input propagation.",
            input_schema={"type": "object"},
        )

    @property
    def risk(self) -> ToolRisk:
        return ToolRisk.EXECUTE

    def validate_input(self, tool_input: JsonObject) -> None:
        del tool_input

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del tool_input, context
        return ToolPermissionResult.allow(
            {"value": "approved"},
            message="Normalized input is safe.",
            reason="test:normalized",
        )

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        del context
        return ToolOutput(str(tool_input["value"]))


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


@pytest.mark.asyncio
async def test_permission_denial_feedback_is_returned_to_model(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / ".nano-code" / "results")
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=FeedbackPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=store,
    )

    result = await executor.execute(
        ToolUseBlock(
            id="write-feedback",
            name="Write",
            input={"path": "a.txt", "content": "no"},
        )
    )

    assert result.is_error is True
    assert "Only explain the proposed change." in result.content
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_bash_subprocess_cannot_inherit_provider_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("NANO_CODE_API_KEY", "generic-must-not-leak")
    executor = build_executor(tmp_path, PermissionMode.BYPASS)

    result = await executor.execute(
        ToolUseBlock(
            id="bash-secret-boundary",
            name="Bash",
            input={
                "command": (
                    "printf '%s %s' \"${ANTHROPIC_API_KEY-unset}\" "
                    '"${NANO_CODE_API_KEY-unset}"'
                )
            },
        )
    )

    assert result.is_error is False
    assert "unset" in result.content
    assert "must-not-leak" not in result.content
    assert "generic-must-not-leak" not in result.content


@pytest.mark.asyncio
async def test_read_only_bash_executes_without_permission_prompt(
    tmp_path: Path,
) -> None:
    executor = ToolExecutor(
        registry=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=FailingPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    result = await executor.execute(
        ToolUseBlock(id="bash-read-only", name="Bash", input={"command": "pwd"})
    )

    assert result.is_error is False
    assert str(tmp_path) in result.content


@pytest.mark.asyncio
async def test_mutating_bash_still_requires_permission(tmp_path: Path) -> None:
    executor = build_executor(tmp_path, PermissionMode.DEFAULT)

    result = await executor.execute(
        ToolUseBlock(
            id="bash-write-denied",
            name="Bash",
            input={"command": "printf changed > created.txt"},
        )
    )

    assert result.is_error is True
    assert "approval was not provided" in result.content
    assert not (tmp_path / "created.txt").exists()


@pytest.mark.asyncio
async def test_executor_runs_the_exact_input_approved_by_tool_policy(
    tmp_path: Path,
) -> None:
    executor = ToolExecutor(
        registry=ToolRegistry([NormalizingTool()]),
        policy=PermissionPolicy(),
        prompter=FailingPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(tmp_path / ".nano-code" / "results"),
    )

    result = await executor.execute(
        ToolUseBlock(
            id="normalized-input",
            name="Normalize",
            input={"value": "model-provided"},
        )
    )

    assert result.is_error is False
    assert result.content == "approved"
