"""Deterministic permission precedence for built-in tools."""

from collections.abc import Iterable

from nano_code.messages import JsonObject
from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
    ToolPermissionBehavior,
    ToolPermissionContext,
)
from nano_code.tools.base import Tool, ToolContext, ToolRisk


class PermissionPolicy:
    """Compose global rules, tool judgments, and mode without performing UI."""

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        rules: Iterable[PermissionRule] = (),
    ) -> None:
        self.mode = mode
        self.rules = tuple(rules)

    async def decide(
        self, tool: Tool, tool_input: JsonObject, context: ToolContext
    ) -> PermissionDecision:
        """Compose global policy with an input-aware tool-local judgment."""

        # Explicit deny and ask rules are checked before every permissive mode.
        # In particular, bypassPermissions must not silently erase user policy.
        deny_rule = self._whole_tool_rule(tool, PermissionBehavior.DENY)
        if deny_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"{tool.definition.name} is denied by an explicit rule.",
                reason=f"rule:{deny_rule.source}",
            )

        ask_rule = self._whole_tool_rule(tool, PermissionBehavior.ASK)
        if ask_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=f"{tool.definition.name} requires confirmation by rule.",
                reason=f"rule:{ask_rule.source}",
            )

        tool_result = await tool.check_permissions(
            tool_input,
            ToolPermissionContext(
                mode=self.mode,
                rules=self.rules,
                tool_context=context,
            ),
        )

        # A tool owns input semantics, so its denial is authoritative. Explicit
        # content asks and protected-path checks can opt out of bypass as well.
        if tool_result.behavior is ToolPermissionBehavior.DENY:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=tool_result.message,
                reason=tool_result.reason,
            )
        if (
            tool_result.behavior is ToolPermissionBehavior.ASK
            and tool_result.bypass_immune
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=tool_result.message,
                reason=tool_result.reason,
            )

        # Plan mode is a capability reduction based on this concrete call, not
        # only a static tool category. Read-only Bash calls therefore remain usable.
        if self.mode is PermissionMode.PLAN and not tool.is_read_only(
            tool_input, context
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"{tool.definition.name} is unavailable in plan mode.",
                reason="mode:plan",
            )

        # Bypass changes the default permission decision, not tool-level safety.
        # Filesystem tools still reject workspace escape and protected paths.
        if self.mode is PermissionMode.BYPASS:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Allowed by bypassPermissions mode.",
                reason="mode:bypassPermissions",
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        # An explicit allow is considered only after higher-priority objections.
        allow_rule = self._whole_tool_rule(tool, PermissionBehavior.ALLOW)
        if allow_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=f"{tool.definition.name} is allowed by an explicit rule.",
                reason=f"rule:{allow_rule.source}",
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        if tool_result.behavior is ToolPermissionBehavior.ALLOW:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=tool_result.message,
                reason=tool_result.reason,
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        if self.mode is PermissionMode.ACCEPT_EDITS and tool.risk is ToolRisk.WRITE:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Workspace edits are allowed by acceptEdits mode.",
                reason="mode:acceptEdits",
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        # dontAsk is fail-closed: an operation that would prompt becomes a denial.
        if self.mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=(
                    f"{tool.definition.name} needs confirmation, "
                    "but prompts are disabled."
                ),
                reason="mode:dontAsk",
            )

        # A normal tool-local ask and passthrough both converge on the same UI
        # boundary. The distinction remains visible in the auditable reason.
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message=tool_result.message,
            reason=tool_result.reason,
            updated_input=tool_result.updated_input,
        )

    def _whole_tool_rule(
        self, tool: Tool, behavior: PermissionBehavior
    ) -> PermissionRule | None:
        for rule in self.rules:
            if (
                rule.tool_name == tool.definition.name
                and rule.behavior is behavior
                and rule.applies_to_entire_tool
            ):
                return rule
        return None


def _updated_input(updated: JsonObject | None, original: JsonObject) -> JsonObject:
    return original if updated is None else updated
