from pathlib import Path

import pytest

from nano_code.messages import JsonObject
from nano_code.permissions import (
    PermissionBehavior,
    PermissionMode,
    PermissionPolicy,
    PermissionRule,
)
from nano_code.tools import ToolContext
from nano_code.tools.builtin.bash import BashTool
from nano_code.tools.builtin.read_file import ReadFileTool
from nano_code.tools.builtin.write_file import WriteFileTool


@pytest.mark.asyncio
async def test_explicit_deny_precedes_bypass_mode(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.DENY)],
    )

    decision = await policy.decide(
        BashTool(), {"command": "pwd"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_explicit_ask_precedes_bypass_mode(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        PermissionMode.BYPASS,
        [PermissionRule("Bash", PermissionBehavior.ASK)],
    )

    decision = await policy.decide(
        BashTool(), {"command": "pwd"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_default_allows_reads_and_asks_for_writes(tmp_path: Path) -> None:
    policy = PermissionPolicy()

    read_input: JsonObject = {"path": "README.md"}
    write_input: JsonObject = {"path": "README.md", "content": "replacement"}
    read = await policy.decide(ReadFileTool(), read_input, ToolContext(tmp_path))
    write = await policy.decide(WriteFileTool(), write_input, ToolContext(tmp_path))

    assert read.behavior is PermissionBehavior.ALLOW
    assert write.behavior is PermissionBehavior.ASK


@pytest.mark.asyncio
async def test_plan_mode_allows_read_only_bash_and_denies_mutation(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(PermissionMode.PLAN)

    read = await policy.decide(
        BashTool(), {"command": "git status"}, ToolContext(tmp_path)
    )
    mutation = await policy.decide(
        BashTool(), {"command": "git add ."}, ToolContext(tmp_path)
    )

    assert read.behavior is PermissionBehavior.ALLOW
    assert mutation.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_content_deny_rule_precedes_read_only_auto_allow(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.DENY,
                source="userSettings",
                rule_content="git:*",
            )
        ]
    )

    decision = await policy.decide(
        BashTool(), {"command": "git status"}, ToolContext(tmp_path)
    )

    assert decision.behavior is PermissionBehavior.DENY
    assert decision.reason == "rule:userSettings"


@pytest.mark.asyncio
async def test_content_allow_rule_can_approve_one_mutating_command(
    tmp_path: Path,
) -> None:
    tool_input: JsonObject = {"command": "uv run pytest"}
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                source="session",
                rule_content="uv run pytest",
            )
        ]
    )

    decision = await policy.decide(BashTool(), tool_input, ToolContext(tmp_path))

    assert decision.behavior is PermissionBehavior.ALLOW
    assert decision.updated_input == tool_input


@pytest.mark.asyncio
async def test_prefix_allow_must_cover_every_compound_subcommand(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                "Bash",
                PermissionBehavior.ALLOW,
                rule_content="git:*",
            )
        ]
    )

    decision = await policy.decide(
        BashTool(),
        {"command": "git status && rm README.md"},
        ToolContext(tmp_path),
    )

    assert decision.behavior is PermissionBehavior.ASK
