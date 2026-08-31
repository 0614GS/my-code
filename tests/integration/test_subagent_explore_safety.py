"""Explore's read-only boundary cannot be bypassed by permission modes or rewrites."""

from pathlib import Path

import pytest

from my_code.conversation.models import ToolCall
from my_code.features.subagents.read_only import ReadOnlyToolProxy
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionRule,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.base import Tool, ToolExecutionContext, ToolOutput
from my_code.tools.builtin.bash import BashTool
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.executor import ToolExecutor
from my_code.workspace.local import Workspace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "rules"),
    (
        (PermissionMode.DEFAULT, ()),
        (
            PermissionMode.DEFAULT,
            (PermissionRule("Bash", PermissionBehavior.ALLOW),),
        ),
        (PermissionMode.BYPASS, ()),
    ),
)
async def test_explore_denies_mutating_bash_in_every_permission_mode(
    tmp_path: Path,
    mode: PermissionMode,
    rules: tuple[PermissionRule, ...],
) -> None:
    tool = ReadOnlyToolProxy(BashTool())
    snapshot = ToolCatalogSnapshot.from_tools((tool,))
    executor = ToolExecutor(
        snapshot,
        PermissionPolicy(mode, rules),
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall("bash-1", "Bash", {"command": "touch forbidden.txt"})
    )

    assert outcome.result.is_error is True
    assert "Explore agents may execute read-only" in outcome.result.content
    assert "not in the read-only command allowlist" in outcome.result.content
    assert not (tmp_path / "forbidden.txt").exists()


@pytest.mark.asyncio
async def test_explore_allows_read_only_bash(tmp_path: Path) -> None:
    tool = ReadOnlyToolProxy(BashTool())
    snapshot = ToolCatalogSnapshot.from_tools((tool,))
    executor = ToolExecutor(
        snapshot,
        PermissionPolicy(PermissionMode.BYPASS),
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    outcome = await executor.execute(ToolCall("bash-1", "Bash", {"command": "pwd"}))

    assert outcome.result.is_error is False
    assert str(tmp_path) in outcome.result.content


class NormalizingTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "Normalize",
            "Normalize input during permission checks.",
            {
                "type": "object",
                "properties": {"mutating": {"type": "boolean"}},
                "required": ["mutating"],
                "additionalProperties": False,
            },
        )

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del context
        return tool_input.get("mutating") is False

    def validate_input(self, tool_input: JsonObject) -> None:
        if not isinstance(tool_input.get("mutating"), bool):
            raise ValueError("mutating must be boolean")

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del tool_input, context
        return ToolPermissionResult.allow(
            {"mutating": True},
            message="normalized",
            reason=PermissionDecisionReason(PermissionDecisionKind.TOOL, "normalized"),
        )

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        del tool_input, context
        return ToolOutput("should not execute")


@pytest.mark.asyncio
async def test_explore_rechecks_normalized_input_before_execute(tmp_path: Path) -> None:
    tool = ReadOnlyToolProxy(NormalizingTool())
    snapshot = ToolCatalogSnapshot.from_tools((tool,))
    executor = ToolExecutor(
        snapshot,
        PermissionPolicy(PermissionMode.BYPASS),
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall("normalize-1", "Normalize", {"mutating": False})
    )

    assert outcome.result.is_error is True
    assert "Explore agents may execute read-only" in outcome.result.content
