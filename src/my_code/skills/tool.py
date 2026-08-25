"""Standard Tool adapter for selecting a lazily indexed Skill."""

from __future__ import annotations

import json
from typing import Protocol

from my_code.model.primitives import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.skills.catalog import SkillCatalogSnapshot
from my_code.skills.models import SkillDefinition, SkillLoadError
from my_code.tools.base import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolOutput,
)
from my_code.tools.validation import required_string

SKILL_TOOL_NAME = "Skill"


class SkillActivator(Protocol):
    def activate(
        self,
        run_id: str,
        snapshot: SkillCatalogSnapshot,
        name: str,
    ) -> SkillDefinition: ...


class SkillTool(Tool):
    """Select metadata now and stage the full instructions for the next step."""

    def __init__(
        self,
        snapshot: SkillCatalogSnapshot,
        activator: SkillActivator,
    ) -> None:
        if not snapshot.entries:
            raise ValueError("Skill tool requires at least one indexed Skill")
        self.snapshot = snapshot
        self._activator = activator
        choices = "\n".join(
            f"- {entry.name}: {entry.description} (source: {entry.source})"
            for entry in snapshot.entries
        )
        self._definition = ModelToolDefinition(
            name=SKILL_TOOL_NAME,
            description=(
                "Activate one indexed Skill. Its validated instructions and optional "
                "tool restriction become available on the next model step only.\n"
                f"Available Skills:\n{choices}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "enum": [entry.name for entry in snapshot.entries],
                        "description": "Stable Skill name to activate",
                    }
                },
                "required": ["skill"],
                "additionalProperties": False,
            },
        )

    @property
    def definition(self) -> ModelToolDefinition:
        return self._definition

    def user_facing_name(self, tool_input: JsonObject) -> str:
        return f"Activate Skill: {tool_input.get('skill', '<invalid>')}"

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Loading Skill {tool_input.get('skill', '<invalid>')}"

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Activating a validated local Skill is allowed.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "validated-skill-index"
            ),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        name = required_string(tool_input, "skill")
        unexpected = set(tool_input) - {"skill"}
        if unexpected:
            raise ValueError(f"Unexpected input field: {sorted(unexpected)[0]}")
        if self.snapshot.get(name) is None:
            raise ValueError(f"Unknown Skill: {name}")

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        if context.run_id is None:
            raise ToolExecutionError("Skill activation requires a run identity")
        name = required_string(tool_input, "skill")
        try:
            definition = self._activator.activate(context.run_id, self.snapshot, name)
        except SkillLoadError as error:
            raise ToolExecutionError(str(error)) from error
        return ToolOutput(
            json.dumps(
                {
                    "skill": definition.name,
                    "source": str(definition.source),
                    "locator": definition.locator,
                    "availableFrom": "next_step",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            metadata={"skill": definition.name},
        )


__all__ = [
    "SKILL_TOOL_NAME",
    "SkillActivator",
    "SkillTool",
]
