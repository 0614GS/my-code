"""Bash tool adapter with foreground-budget handoff to supervised tasks."""

from typing import Protocol

from my_code.conversation.presentation import ToolResultPresentation
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateDestination,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.updates import permission_rule_for_destination
from my_code.tools.base import (
    ReadOnlyAssessment,
    Tool,
    ToolExecutionContext,
    ToolOutput,
)
from my_code.tools.builtin.bash.permissions import (
    allowing_rules,
    analyze_bash_command,
    matching_rule,
    suggest_bash_permission,
)
from my_code.tools.builtin.bash.process import execute_bash
from my_code.tools.presentation import compact_text
from my_code.tools.validation import optional_int, required_string
from my_code.workspace.launcher import CommandAuthority


def _rule_reason(rule: PermissionRule) -> PermissionDecisionReason:
    return PermissionDecisionReason(
        PermissionDecisionKind.RULE,
        f"Bash:{rule.behavior.value}",
        rule=rule,
    )


class BashBackgroundExecutor(Protocol):
    async def execute(
        self,
        command: str,
        context: ToolExecutionContext,
        foreground_budget: float,
        *,
        background: bool,
        authority: str = "use_default",
        escalation_available: bool = False,
    ) -> ToolOutput: ...


class BashTool(Tool):
    def __init__(
        self,
        *,
        background_executor: BashBackgroundExecutor | None = None,
        background_enabled: bool = False,
        execution_environment: str = "local",
        sandboxed: bool = False,
        escalation_enabled: bool = False,
    ) -> None:
        self.background_executor = background_executor
        self.background_enabled = background_enabled
        self.execution_environment = execution_environment
        self.sandboxed = sandboxed
        self.escalation_enabled = escalation_enabled

    def foreground_only(self) -> "BashTool":
        """Return the hard-timeout Bash capability used by child agents."""

        return BashTool(
            execution_environment=self.execution_environment,
            sandboxed=self.sandboxed,
            escalation_enabled=self.escalation_enabled,
        )

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Bash",
            description=(
                "Run a shell command in the workspace. Commands are permission-gated "
                f"and execute via {self.execution_environment}. "
                "The shell already starts in the workspace; "
                "do not prefix commands with cd to that same directory."
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
                    **(
                        {
                            "sandbox_permissions": {
                                "type": "string",
                                "enum": ["use_default", "require_escalated"],
                                "default": "use_default",
                                "description": (
                                    "Use require_escalated only when this command "
                                    "must run outside the active OS sandbox."
                                ),
                            },
                            "justification": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                                "description": (
                                    "Why this one command must leave the sandbox."
                                ),
                            },
                        }
                        if self.escalation_enabled
                        else {}
                    ),
                    **(
                        {
                            "background": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "When true, run asynchronously and return a task "
                                    "ID immediately. When false or omitted, wait for "
                                    "completion up to the timeout; if that wait "
                                    "expires, "
                                    "the command continues in the background."
                                ),
                            }
                        }
                        if self.background_enabled
                        else {}
                    ),
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

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        command = required_string(tool_input, "command")
        return analyze_bash_command(command, context.cwd).is_read_only

    def assess_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ReadOnlyAssessment:
        command = required_string(tool_input, "command")
        analysis = analyze_bash_command(command, context.cwd)
        reason = (
            analysis.reason
            if analysis.is_read_only or not analysis.is_workspace_edit
            else "command is not in the read-only command allowlist"
        )
        return ReadOnlyAssessment(analysis.is_read_only, reason)

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        """为权限策略解释 Bash 专属规则和命令语义。"""

        command = required_string(tool_input, "command")
        analysis = analyze_bash_command(command, context.workspace_root)
        elevated = tool_input.get("sandbox_permissions") == "require_escalated"
        suggestion = suggest_bash_permission(command, context.workspace_root)
        remember_update = PermissionUpdate.add_rules(
            (
                permission_rule_for_destination(
                    "Bash",
                    PermissionBehavior.ALLOW,
                    PermissionUpdateDestination.LOCAL,
                    suggestion.rule_content,
                ),
            ),
            destination=PermissionUpdateDestination.LOCAL,
        )

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

        if elevated:
            if context.mode is PermissionMode.PLAN:
                return ToolPermissionResult.deny(
                    message="Sandbox escalation is unavailable in plan mode.",
                    reason=PermissionDecisionReason(
                        PermissionDecisionKind.MODE, "plan-sandbox-escalation"
                    ),
                )
            if context.mode is PermissionMode.DONT_ASK:
                return ToolPermissionResult.deny(
                    message=(
                        "Sandbox escalation requires interactive confirmation, "
                        "but prompts are disabled."
                    ),
                    reason=PermissionDecisionReason(
                        PermissionDecisionKind.MODE, "dontAsk-sandbox-escalation"
                    ),
                )
            return ToolPermissionResult.ask(
                message=(
                    "This command requests full host-user authority outside the "
                    "sandbox. It can access host files, network, processes, and "
                    "write protected .git/.my-code metadata. "
                    f"Justification: {tool_input['justification']}"
                ),
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.SAFETY, "sandbox-escalation"
                ),
                bypass_immune=True,
            )

        if context.mode is PermissionMode.PLAN and not analysis.is_read_only:
            return ToolPermissionResult.deny(
                message=(
                    "Only Bash commands proven read-only by static analysis are "
                    "available in plan mode."
                ),
                reason=PermissionDecisionReason(PermissionDecisionKind.MODE, "plan"),
            )

        allow_rules = context.rules_for(self.definition.name, PermissionBehavior.ALLOW)
        matched_allow_rules = allowing_rules(
            analysis, command, allow_rules, context.workspace_root
        )
        local_allow_rules = tuple(
            rule
            for rule in allow_rules
            if rule.source == PermissionUpdateDestination.LOCAL.value
        )
        local_matches = allowing_rules(
            analysis, command, local_allow_rules, context.workspace_root
        )
        local_remembered_allow = bool(local_matches)

        ask_rule = matching_rule(
            analysis,
            command,
            context.rules_for(self.definition.name, PermissionBehavior.ASK),
        )
        if ask_rule is not None and not local_remembered_allow:
            return ToolPermissionResult.ask(
                message=(
                    f"Bash command requires confirmation by a {ask_rule.source} rule."
                ),
                reason=_rule_reason(ask_rule),
                bypass_immune=True,
                suggestions=(remember_update,),
            )

        if self.sandboxed:
            return ToolPermissionResult.allow(
                tool_input,
                message="Bash command is contained by the active OS sandbox.",
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.SAFETY, "sandbox-default-authority"
                ),
            )

        if matched_allow_rules:
            effective_allow_rules = (
                local_matches if local_remembered_allow else matched_allow_rules
            )
            sources = ", ".join(
                dict.fromkeys(rule.source for rule in effective_allow_rules)
            )
            return ToolPermissionResult.allow(
                tool_input,
                message=f"Bash command is allowed by {sources} rule(s).",
                reason=_rule_reason(effective_allow_rules[0]),
                overrides_ask=local_remembered_allow,
            )

        if analysis.is_read_only:
            return ToolPermissionResult.allow(
                tool_input,
                message="Bash command was proven read-only.",
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.TOOL, "bash-read-only"
                ),
            )
        if context.mode is PermissionMode.ACCEPT_EDITS and analysis.is_workspace_edit:
            return ToolPermissionResult.allow(
                tool_input,
                message="Bash command is a proven safe workspace edit.",
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.MODE, "acceptEdits"
                ),
            )
        return ToolPermissionResult.passthrough(
            message=f"Allow Bash for this call? {analysis.reason}.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "bash-approval-required"
            ),
            suggestions=(remember_update,),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        command = required_string(tool_input, "command")
        if len(command) > 50_000:
            raise ValueError("'command' exceeds 50,000 characters")
        optional_int(tool_input, "timeout", 120, minimum=1, maximum=600)
        background = tool_input.get("background", False)
        if "background" in tool_input and not self.background_enabled:
            raise ValueError("'background' is unavailable in this Bash context")
        if not isinstance(background, bool):
            raise ValueError("'background' must be a boolean")
        permission = tool_input.get("sandbox_permissions", "use_default")
        if not self.escalation_enabled and (
            "sandbox_permissions" in tool_input or "justification" in tool_input
        ):
            raise ValueError("sandbox escalation is unavailable in this Bash context")
        if permission not in {"use_default", "require_escalated"}:
            raise ValueError(
                "'sandbox_permissions' must be 'use_default' or 'require_escalated'"
            )
        justification = tool_input.get("justification")
        if permission == "require_escalated":
            if not isinstance(justification, str) or not justification.strip():
                raise ValueError("'justification' is required for require_escalated")
            if len(justification) > 2000:
                raise ValueError("'justification' exceeds 2,000 characters")
        elif "justification" in tool_input:
            raise ValueError("'justification' is only valid with require_escalated")

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        command = required_string(tool_input, "command")
        timeout = optional_int(
            tool_input,
            "timeout",
            round(context.command_timeout_seconds),
            minimum=1,
            maximum=600,
        )
        if self.background_executor is None:
            return await execute_bash(
                command,
                context,
                timeout,
                authority=_authority(tool_input),
                escalation_available=self.escalation_enabled,
            )
        return await self.background_executor.execute(
            command,
            context,
            timeout,
            background=tool_input.get("background", False) is True,
            authority=_authority(tool_input),
            escalation_available=self.escalation_enabled,
        )


def _authority(tool_input: JsonObject) -> CommandAuthority:
    if tool_input.get("sandbox_permissions") == "require_escalated":
        return CommandAuthority.REQUIRE_ESCALATED
    return CommandAuthority.USE_DEFAULT
