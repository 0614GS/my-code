"""Execute an explicitly permitted shell command in the workspace."""

import asyncio
import os
import signal

from nano_code.messages import JsonObject
from nano_code.permissions import (
    PermissionBehavior,
    PermissionRule,
    ToolPermissionContext,
    ToolPermissionResult,
)
from nano_code.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolOutput,
    ToolRisk,
)
from nano_code.tools.builtin.bash_permissions import (
    BashAnalysis,
    analyze_bash_command,
    bash_rule_matches,
)
from nano_code.tools.validation import optional_int, required_string


class BashTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
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

    @property
    def risk(self) -> ToolRisk:
        return ToolRisk.EXECUTE

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        command = required_string(tool_input, "command")
        return analyze_bash_command(command, context.cwd).is_read_only

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        """Interpret Bash-specific rules and command semantics for the policy."""

        command = required_string(tool_input, "command")
        analysis = analyze_bash_command(command, context.tool_context.cwd)

        deny_rule = self._matching_rule(
            analysis, command, context, PermissionBehavior.DENY
        )
        if deny_rule is not None:
            return ToolPermissionResult.deny(
                message=f"Bash command is denied by a {deny_rule.source} rule.",
                reason=f"rule:{deny_rule.source}",
            )

        ask_rule = self._matching_rule(
            analysis, command, context, PermissionBehavior.ASK
        )
        if ask_rule is not None:
            return ToolPermissionResult.ask(
                message=(
                    f"Bash command requires confirmation by a {ask_rule.source} rule."
                ),
                reason=f"rule:{ask_rule.source}",
                bypass_immune=True,
            )

        allow_rules = context.rules_for(self.definition.name, PermissionBehavior.ALLOW)
        matched_allow_rules = self._allowing_rules(analysis, command, allow_rules)
        if matched_allow_rules:
            sources = ", ".join(
                dict.fromkeys(rule.source for rule in matched_allow_rules)
            )
            return ToolPermissionResult.allow(
                tool_input,
                message=f"Bash command is allowed by {sources} rule(s).",
                reason=f"rule:{sources}",
            )

        if analysis.is_read_only:
            return ToolPermissionResult.allow(
                tool_input,
                message="Bash command was proven read-only.",
                reason="bash:read-only",
            )
        return ToolPermissionResult.passthrough(
            message=f"Allow Bash for this call? {analysis.reason}.",
            reason="bash:approval-required",
        )

    def _matching_rule(
        self,
        analysis: BashAnalysis,
        command: str,
        context: ToolPermissionContext,
        behavior: PermissionBehavior,
    ) -> PermissionRule | None:
        for rule in context.rules_for(self.definition.name, behavior):
            assert rule.rule_content is not None
            if bash_rule_matches(rule.rule_content, command) or any(
                bash_rule_matches(rule.rule_content, subcommand)
                for subcommand in analysis.commands
            ):
                return rule
        return None

    @staticmethod
    def _allowing_rules(
        analysis: BashAnalysis,
        command: str,
        rules: tuple[PermissionRule, ...],
    ) -> tuple[PermissionRule, ...]:
        # Exact rules may intentionally approve a complex full command. Prefix
        # rules must be checked per subcommand, otherwise Bash(git:*) could also
        # approve ``git status && rm file`` because the full string starts git.
        exact = tuple(
            rule
            for rule in rules
            if rule.rule_content is not None
            and not rule.rule_content.rstrip().endswith((":*", " *"))
            and bash_rule_matches(rule.rule_content, command)
        )
        if exact:
            return exact
        if not analysis.commands:
            return ()

        matched: list[PermissionRule] = []
        for subcommand in analysis.commands:
            rule = next(
                (
                    candidate
                    for candidate in rules
                    if candidate.rule_content is not None
                    and bash_rule_matches(candidate.rule_content, subcommand)
                ),
                None,
            )
            if rule is None:
                return ()
            matched.append(rule)
        return tuple(matched)

    def validate_input(self, tool_input: JsonObject) -> None:
        command = required_string(tool_input, "command")
        if len(command) > 50_000:
            raise ValueError("'command' exceeds 50,000 characters")
        optional_int(tool_input, "timeout", 120, minimum=1, maximum=600)

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        # Permission has already been granted by ToolExecutor. This process is still
        # not sandboxed: cwd scopes convenience, not operating-system capabilities.
        command = required_string(tool_input, "command")
        timeout = optional_int(
            tool_input,
            "timeout",
            round(context.command_timeout_seconds),
            minimum=1,
            maximum=600,
        )
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=context.cwd,
            # The provider needs its credential, but agent-controlled child
            # processes do not. Strip it even when the user chose an environment
            # override so a Bash call cannot print the parent process's API key.
            env=_subprocess_environment(),
            # Merge streams to preserve the command's observable output order.
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # A separate process group lets cancellation reach shell descendants,
            # not just the immediate shell process.
            start_new_session=os.name != "nt",
        )
        try:
            output = await asyncio.wait_for(
                self._collect_output(process, context.max_command_output_bytes),
                timeout=timeout,
            )
        except TimeoutError as error:
            await self._terminate(process)
            raise ToolExecutionError(f"Command timed out after {timeout}s") from error
        except asyncio.CancelledError:
            # Never leave a command running after the owning agent turn is cancelled.
            await self._terminate(process)
            raise
        except BaseException:
            await self._terminate(process)
            raise

        exit_code = await process.wait()
        text = output.decode("utf-8", errors="replace")
        if not text:
            text = "<no output>"
        return ToolOutput(
            content=f"exit_code: {exit_code}\n{text}",
            is_error=exit_code != 0,
        )

    @staticmethod
    async def _collect_output(process: asyncio.subprocess.Process, limit: int) -> bytes:
        if process.stdout is None:
            raise ToolExecutionError("Shell process has no stdout pipe")
        chunks: list[bytes] = []
        size = 0
        while chunk := await process.stdout.read(64 * 1024):
            size += len(chunk)
            if size > limit:
                # Bound memory before the generic tool-result externalizer runs.
                raise ToolExecutionError(
                    f"Command output exceeded {limit // (1024 * 1024)} MiB"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            # Give the process group a brief graceful shutdown window, then escalate
            # to kill so timeout/cancellation cannot leak background processes.
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                process.kill()
                await process.wait()


def _subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("NANO_CODE_API_KEY", None)
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("ANTHROPIC_AUTH_TOKEN", None)
    return environment
