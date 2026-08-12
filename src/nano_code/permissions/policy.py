"""Deterministic permission precedence for built-in tools."""

from collections.abc import Iterable

from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
)
from nano_code.tools.base import Tool, ToolRisk


class PermissionPolicy:
    """Resolve tool metadata and exact rules without performing UI input."""

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        rules: Iterable[PermissionRule] = (),
    ) -> None:
        self.mode = mode
        self.rules = tuple(rules)

    def decide(self, tool: Tool) -> PermissionDecision:
        """Apply deny/ask, mode, allow, then the safe default in that order."""

        deny_rule = self._rule(tool, PermissionBehavior.DENY)
        if deny_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"{tool.definition.name} is denied by an explicit rule.",
                reason=f"rule:{deny_rule.source}",
            )

        ask_rule = self._rule(tool, PermissionBehavior.ASK)
        if ask_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=f"{tool.definition.name} requires confirmation by rule.",
                reason=f"rule:{ask_rule.source}",
            )

        if self.mode is PermissionMode.PLAN and tool.risk is not ToolRisk.READ:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"{tool.definition.name} is unavailable in plan mode.",
                reason="mode:plan",
            )

        if self.mode is PermissionMode.BYPASS:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Allowed by bypassPermissions mode.",
                reason="mode:bypassPermissions",
            )

        allow_rule = self._rule(tool, PermissionBehavior.ALLOW)
        if allow_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=f"{tool.definition.name} is allowed by an explicit rule.",
                reason=f"rule:{allow_rule.source}",
            )

        if tool.risk is ToolRisk.READ:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Read-only workspace tools are allowed by default.",
                reason="risk:read",
            )

        if self.mode is PermissionMode.ACCEPT_EDITS and tool.risk is ToolRisk.WRITE:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Workspace edits are allowed by acceptEdits mode.",
                reason="mode:acceptEdits",
            )

        if self.mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=(
                    f"{tool.definition.name} needs confirmation, "
                    "but prompts are disabled."
                ),
                reason="mode:dontAsk",
            )

        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message=f"Allow {tool.definition.name} for this call?",
            reason="default:ask",
        )

    def _rule(self, tool: Tool, behavior: PermissionBehavior) -> PermissionRule | None:
        for rule in self.rules:
            if rule.tool_name == tool.definition.name and rule.behavior is behavior:
                return rule
        return None
