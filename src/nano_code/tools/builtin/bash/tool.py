"""Bash tool adapter."""

from nano_code.model import JsonObject, ModelToolDefinition
from nano_code.permissions import (
    PermissionBehavior,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionRule,
    ToolPermissionContext,
    ToolPermissionResult,
)
from nano_code.tools.base import (
    Tool,
    ToolContext,
    ToolOutput,
)
from nano_code.tools.builtin.bash.permissions import (
    allowing_rules,
    analyze_bash_command,
    matching_rule,
)
from nano_code.tools.builtin.bash.process import execute_bash
from nano_code.tools.presentation import ToolResultPresentation, compact_text
from nano_code.tools.validation import optional_int, required_string


def _rule_reason(rule: PermissionRule) -> PermissionDecisionReason:
    return PermissionDecisionReason(
        PermissionDecisionKind.RULE,
        f"Bash:{rule.behavior.value}",
        rule=rule,
    )


class BashTool(Tool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Bash",
            description=(
                "Run a shell command in the workspace. Commands are permission-gated "
                "but are not OS-sandboxed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 600,
                        "description": "Timeout in seconds",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return compact_text(required_string(tool_input, "command"))

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Running {self.get_tool_use_summary(tool_input)}"

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        del tool_input
        exit_code = output.metadata.get("exit_code")
        preview = output.metadata.get("preview")
        if isinstance(exit_code, int) and isinstance(preview, str):
            return ToolResultPresentation(
                summary=compact_text(f"exit_code: {exit_code} · {preview}"),
                truncated=bool(output.metadata.get("has_more_output")),
            )
        return super().present_result({}, output)

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        command = required_string(tool_input, "command")
        return analyze_bash_command(command, context.cwd).is_read_only

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        """为权限策略解释 Bash 专属规则和命令语义。"""

        command = required_string(tool_input, "command")
        analysis = analyze_bash_command(command, context.workspace_root)

        deny_rule = matching_rule(
            analysis,
            command,
            context.rules_for(self.definition.name, PermissionBehavior.DENY),
        )
        if deny_rule is not None:
            return ToolPermissionResult.deny(
                message=f"Bash command is denied by a {deny_rule.source} rule.",
                reason=_rule_reason(deny_rule),
            )

        ask_rule = matching_rule(
            analysis,
            command,
            context.rules_for(self.definition.name, PermissionBehavior.ASK),
        )
        if ask_rule is not None:
            return ToolPermissionResult.ask(
                message=(
                    f"Bash command requires confirmation by a {ask_rule.source} rule."
                ),
                reason=_rule_reason(ask_rule),
                bypass_immune=True,
            )

        if context.mode is PermissionMode.PLAN and not analysis.is_read_only:
            return ToolPermissionResult.deny(
                message="Mutating Bash commands are unavailable in plan mode.",
                reason=PermissionDecisionReason(PermissionDecisionKind.MODE, "plan"),
            )

        allow_rules = context.rules_for(self.definition.name, PermissionBehavior.ALLOW)
        matched_allow_rules = allowing_rules(
            analysis, command, allow_rules, context.workspace_root
        )
        if matched_allow_rules:
            sources = ", ".join(
                dict.fromkeys(rule.source for rule in matched_allow_rules)
            )
            return ToolPermissionResult.allow(
                tool_input,
                message=f"Bash command is allowed by {sources} rule(s).",
                reason=_rule_reason(matched_allow_rules[0]),
            )

        if analysis.is_read_only:
            return ToolPermissionResult.allow(
                tool_input,
                message="Bash command was proven read-only.",
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.TOOL, "bash-read-only"
                ),
            )
        return ToolPermissionResult.passthrough(
            message=f"Allow Bash for this call? {analysis.reason}.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "bash-approval-required"
            ),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        command = required_string(tool_input, "command")
        if len(command) > 50_000:
            raise ValueError("'command' exceeds 50,000 characters")
        optional_int(tool_input, "timeout", 120, minimum=1, maximum=600)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        command = required_string(tool_input, "command")
        timeout = optional_int(
            tool_input,
            "timeout",
            round(context.command_timeout_seconds),
            minimum=1,
            maximum=600,
        )
        return await execute_bash(command, context, timeout)
