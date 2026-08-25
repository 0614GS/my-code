"""Skill runtime publication, reload, and normalized external sources."""

from pathlib import Path

import pytest

from my_code.skills.discovery import SkillSearchRoot
from my_code.skills.models import (
    SkillDefinition,
    SkillMetadata,
    SkillSourceId,
    SkillSourceKind,
)
from my_code.skills.runtime import SkillRuntime
from my_code.tools.catalog import ToolCatalog


def _write_skill(root: Path, name: str, body: str = "instructions") -> None:
    target = root / name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\ndescription: {name} description\n---\n{body}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_reload_atomically_replaces_skill_tool_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    tools = ToolCatalog()
    runtime = SkillRuntime(
        enabled=True,
        roots=(
            SkillSearchRoot(
                SkillSourceId(300, SkillSourceKind.PROJECT, "workspace"), root
            ),
        ),
        tool_catalog=tools,
    )
    await runtime.start()
    old_tool = tools.snapshot().get("Skill")
    assert old_tool is not None

    _write_skill(root, "beta")
    runtime.reload()
    new_tool = tools.snapshot().get("Skill")

    assert new_tool is not None and new_tool is not old_tool
    assert old_tool.definition.input_schema["properties"]["skill"]["enum"] == [  # type: ignore[index]
        "alpha"
    ]
    assert new_tool.definition.input_schema["properties"]["skill"]["enum"] == [  # type: ignore[index]
        "alpha",
        "beta",
    ]
    await runtime.close()
    assert tools.snapshot().get("Skill") is None


@pytest.mark.asyncio
async def test_external_source_uses_same_definition_and_conflict_rules() -> None:
    tools = ToolCatalog()
    runtime = SkillRuntime(enabled=True, roots=(), tool_catalog=tools)
    await runtime.start()
    source = SkillSourceId(150, SkillSourceKind.MCP, "docs-server")

    snapshot = runtime.replace_source(
        source,
        (
            SkillDefinition(
                SkillMetadata("remote", "remote metadata"),
                "remote instructions",
                source,
                "mcp://docs-server/skills/remote",
            ),
        ),
    )

    assert snapshot.load("remote").instructions == "remote instructions"
    assert tools.snapshot().get("Skill") is not None
    await runtime.close()
