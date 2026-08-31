"""Standard Tool adapter for selecting a lazily indexed Skill."""

from __future__ import annotations

from typing import Protocol

from my_code.conversation.attachments import (
    InvokedSkillsAttachment,
    SkillActivationAttachment,
)
from my_code.conversation.models import AttachmentMessage, ConversationEntry
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
    PermissionUpdateType,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.rules import parse_permission_rule
from my_code.skills.catalog import SkillCatalogSnapshot
from my_code.skills.models import SkillDefinition, SkillLoadError
from my_code.tools.base import (
    Tool,
    ToolExecutionContext,
    ToolExecutionError,
    ToolOutput,
)
from my_code.tools.validation import required_string

SKILL_TOOL_NAME = "Skill"


class SkillActivator(Protocol):
    def activate(
        self,
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
        self._definition = ModelToolDefinition(
            name=SKILL_TOOL_NAME,
            description=(
                "Activate an available Skill by its stable name. The Skill listing "
                "is provided separately in conversation context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
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

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        name = required_string(tool_input, "skill")
        entry = self.snapshot.get(name)
        if entry is not None and entry.allowed_tools:
            return ToolPermissionResult.ask(
                message=(f"Skill {name} requests additive session tool permissions."),
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.TOOL, "skill-session-permissions"
                ),
                bypass_immune=True,
                updated_input=tool_input,
            )
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

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        del context
        name = required_string(tool_input, "skill")
        try:
            definition = self._activator.activate(self.snapshot, name)
        except SkillLoadError as error:
            raise ToolExecutionError(str(error)) from error
        attachment = SkillActivationAttachment(
            definition.name,
            definition.instructions,
            str(definition.source),
            definition.locator,
            definition.compatibility,
            definition.allowed_tools or (),
        )
        updates = _permission_updates(attachment)
        return ToolOutput(
            f"Launching skill: {definition.name}",
            metadata={"skill": definition.name},
            new_attachments=(attachment,),
            permission_updates=updates,
        )


def _permission_updates(
    attachment: SkillActivationAttachment,
) -> tuple[PermissionUpdate, ...]:
    if not attachment.allowed_tools:
        return ()
    rules = tuple(
        PermissionRule(
            tool_name,
            PermissionBehavior.ALLOW,
            content,
            source=PermissionUpdateDestination.SESSION.value,
        )
        for rule in attachment.allowed_tools
        for tool_name, content in (parse_permission_rule(rule),)
    )
    return (
        PermissionUpdate(
            PermissionUpdateType.ADD_RULES,
            PermissionUpdateDestination.SESSION,
            rules=rules,
            behavior=PermissionBehavior.ALLOW,
        ),
    )


def restore_skill_permissions(
    policy: PermissionPolicy, history: tuple[ConversationEntry, ...]
) -> None:
    """Rebuild additive Skill grants from durable conversation facts."""

    policy.rules = tuple(
        rule
        for rule in policy.rules
        if rule.source != PermissionUpdateDestination.SESSION.value
    )
    latest: dict[str, SkillActivationAttachment] = {}
    for message in history:
        if not isinstance(message, AttachmentMessage):
            continue
        payload = message.payload
        if isinstance(payload, SkillActivationAttachment):
            latest[payload.name] = payload
        elif isinstance(payload, InvokedSkillsAttachment):
            for skill in payload.skills:
                latest[skill.name] = skill
    for attachment in latest.values():
        for update in _permission_updates(attachment):
            policy.apply_update(update)


__all__ = [
    "SKILL_TOOL_NAME",
    "SkillActivator",
    "SkillTool",
    "restore_skill_permissions",
]
